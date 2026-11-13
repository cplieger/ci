"""Per-mutant mutation cost from a recorded run, and the shard packing of both weekly mutation runs.

gremlins and Stryker each report one completion per mutant while a fixed pool of
workers runs the mutants. A mutant's measured cost is the gap before its
completion times the workers (attribute), kept per file blob and mutant identity.
The next run prices each unit as its measured mutants' seconds plus its tool's
prior times PRIOR_ERROR for the rest (price), and packs units into shards that each
end near a target (choose), more than MAX_SHARDS only where the bound on every job
needs them (fit).
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from string.templatelib import Template, convert
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from pathlib import Path

# Every plain log line starts with this, so the runner never reads one as a command.
SAY_PREFIX = '- '

# The bar a sharded run meets (bar_faults): the run's measured time within BAND of
# its projection, no shard more than BAND over its own, and every job, slowest
# runner included, ended by SAFE_JOB_MINUTES, 60 min under the hosted runner's
# 360-min job limit.
BAND = 0.15
SAFE_JOB_MINUTES = 300
# The largest ratio of two jobs' times on identical work, rounded up to 0.1: 1.9 in
# gremlins run 43328219246 (shard 2's work took 85, 125 and 161 min), past the 1.76
# of run 43153682226. gremlins-shard.py's record job measures it each week and warns past this.
SLOWDOWN = 1.9
# A mutant with no measure at its file's current git blob is planned at its tool's prior times
# this, a ruled margin: in Stryker run 43328238671 prior-priced work took at most 0.48x its prior
# per shard, and 3.53x in the largest single-file ratio (platform.ts, too small to move a shard).
# Both weekly records measure it per shard (prior_error) and warn past this.
PRIOR_ERROR = 3.0
# A job matrix runs at most 256 jobs (https://docs.github.com/en/actions/reference/limits),
# so a plan whose run matrix needs more fails.
MATRIX_LIMIT = 256
# The most shards a plan takes to bring its shards into the band (choose); fit takes more
# only for the bound. deadset-ts's full Stryker run, about 36 machine-hours, needs about
# 16 to 19 shards at the band's top (5 to 31 min of setup).
MAX_SHARDS = 16
# A float holds every int up to this exactly; a larger count is not a measure.
MAX_COUNT = 2**53
# gremlins-run.sh times a suite with `date +%s` and a package's mutant in whole
# milliseconds. A probe under its clock's tick measured nothing, so it is unknown.
SECOND_TICK = 1.0
MILLISECOND_TICK = 0.001
# A run plan or cost history holds about 200 bytes a file, so this is over
# 100,000 files; a larger input is refused unparsed.
MAX_INPUT_BYTES = 32 * 2**20
# GitHub's repository name characters, and a directory part's. Neither set
# holds '~' or '=', which entry_key uses as its separators.
REPO_NAME = re.compile(r'[A-Za-z0-9._-]+')
DIR_PART = re.compile(r'[A-Za-z0-9._-]+')
# A git object id, SHA-1 or SHA-256: the file version a cost history's measures belong to.
GIT_BLOB = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')


def entry_key(repo: str, dir_: str | None = None) -> str:
    """The name every artifact, plan and history file of one entry is built from, distinct per (REPO, DIR_).

    A whole-repository entry (DIR_ None) is its name; a package dir's is
    REPO~DIR with '/' written '=', and the root's REPO~. Only the characters
    above go in, so the key reads back to one entry and holds no glob
    character. Raises ValueError for any other name.
    """
    if not isinstance(repo, str) or not REPO_NAME.fullmatch(repo) or repo in ('.', '..'):
        raise ValueError(f'repository name {repo!r:.200} is outside [A-Za-z0-9._-]')
    if dir_ is None:
        return repo
    if dir_ == '.':
        return f'{repo}~'
    parts = dir_.split('/') if isinstance(dir_, str) else ['']
    if not all(DIR_PART.fullmatch(p) and p not in ('.', '..') for p in parts):
        raise ValueError(f'directory {dir_!r:.200} is not a relative path of [A-Za-z0-9._-] names')
    return f'{repo}~{"=".join(parts)}'


@dataclass(frozen=True)
class Item:
    """One unit a shard runs whole: a file, or a group with nothing to split."""

    group: str  # what one setup buys: a gremlins module, a Stryker entry
    unit: str | None
    work: float  # wall seconds on the group's workers
    known: float | None = None  # the part of work a measure priced; None for all of it
    worst: str | None = (
        None  # its costliest measured mutant, named when the measures alone break the bound
    )


@dataclass
class Bin:
    load: float = 0.0
    groups: set[str] = field(default_factory=set)
    items: list[Item] = field(default_factory=list)


def pack(items: list[Item], k: int, setup: dict[str, float]) -> list[Bin]:
    """Greedy longest-first packing into k shards.

    Each item goes to the shard whose projected time it raises least, counting
    its group's setup only where that group is not yet present; ties go to a
    shard that already runs the group, then to the lower index. With one group
    this is the classic longest-processing-time rule.
    """
    bins = [Bin() for _ in range(k)]
    groups = {it.group for it in items}
    # Per group, (load, index) heaps of the shards that run it and of those that
    # do not; an entry whose load or membership has since changed is skipped.
    have: dict[str, list[tuple[float, int]]] = {g: [] for g in groups}
    lack: dict[str, list[tuple[float, int]]] = {g: [(0.0, i) for i in range(k)] for g in groups}

    def lowest(heap: list[tuple[float, int]], g: str, *, member: bool) -> tuple[float, int] | None:
        while heap and (
            heap[0][0] != bins[heap[0][1]].load or (g in bins[heap[0][1]].groups) != member
        ):
            heapq.heappop(heap)
        return heap[0] if heap else None

    for item in sorted(items, key=lambda it: (-it.work, it.group, it.unit or '')):
        g = item.group
        options = []
        if top := lowest(have[g], g, member=True):
            options.append((top[0] + item.work, False, top[1]))
        if top := lowest(lack[g], g, member=False):
            options.append((top[0] + item.work + setup[g], True, top[1]))
        load, _, i = min(options)
        b = bins[i]
        b.load = load
        b.groups.add(g)
        b.items.append(item)
        for h in groups:
            heapq.heappush(have[h] if h in b.groups else lack[h], (load, i))
    return bins


# The most one job's setup and work may project so a SLOWDOWN-times slower runner, which
# stretches the setup as well, ends it by SAFE_JOB_MINUTES.
BOUND_SECS = SAFE_JOB_MINUTES * 60 / SLOWDOWN


# The band's top, setup and work together: a shard whose jobs average BAND over a projection
# this long still holds BOUND_SECS, whatever its setup.
TOP_SECS = SAFE_JOB_MINUTES * 60 / (SLOWDOWN * (1 + BAND))


def bar_faults(projected: list[float], attempts: list[list[float | None]]) -> list[str]:
    """How a run misses the bar, or []: PROJECTED minutes per shard, and per attempt each shard's measured minutes.

    Bar (a) never judges one job. The run's measured time, the sum of its shards'
    means over the attempts, lands within BAND of its projected total either way,
    and with two or more shards no shard's mean over two or more attempts runs
    more than BAND over its projection. A shard under its projection costs only
    parallelism, so it is no fault. Bar (b) judges every job. A shard with no
    measure in some attempt has no mean, which misses the bar.
    """
    faults = []
    means: list[float] = []
    for i, want in enumerate(projected):
        got = [a[i] for a in attempts if i < len(a) and a[i] is not None]
        if len(got) < len(attempts) or not got:
            faults.append(f'shard {i + 1} has no measure in every attempt')
            continue
        mean = sum(got) / len(got)
        means.append(mean)
        if len(projected) > 1 and len(got) > 1 and mean > want * (1 + BAND):
            faults.append(f'shard {i + 1} averaged {mean:.1f} min against {want:.1f} projected')
        if max(got) > SAFE_JOB_MINUTES:
            faults.append(f'shard {i + 1} took {max(got):.1f} min, past {SAFE_JOB_MINUTES}')
    if len(means) == len(projected) and len(projected) * len(attempts) > 1:
        want, got = sum(projected), sum(means)
        if abs(got - want) > want * BAND:
            verb = 'took' if len(attempts) == 1 else 'averaged'
            faults.append(
                f'the run {verb} {got:.1f} min over its shards against {want:.1f} projected'
            )
    return faults


def floor_secs(items: list[Item], setup: dict[str, float]) -> float:
    """The slowest one item and its setup can run, which no shard count shortens."""
    return max((it.work + setup[it.group] for it in items), default=0.0)


def choose(
    items: list[Item], setup: dict[str, float], top: float, limit: int = MAX_SHARDS
) -> tuple[int, list[Bin]]:
    """The shard count for a band whose top is TOP seconds (TOP_SECS), at most LIMIT, and its packing.

    The band runs from TOP x (1 - BAND) / (1 + BAND) to TOP. One job when it
    ends by the top; else the fewest shards that all land inside the band, else
    the fewest whose slowest ends by the top. When no count gets there, because
    an item and its setup pass the top or LIMIT stops the search first, the
    fewest shards in which only an item running alone passes the top, or
    failing that the fewest whose slowest shard of several items (crowded) is
    lowest: no count shortens an item running alone.
    """
    high, low = top, top * (1 - BAND) / (1 + BAND)
    one = pack(items, 1, setup)
    if one[0].load <= high:
        return 1, one
    floor = floor_secs(items, setup)
    limit = min(limit, max(1, len(items)))
    if one[0].load <= floor or limit == 1:
        return 1, one
    total, setups = sum(it.work for it in items), sum(setup[g] for g in {it.group for it in items})
    # Every shard count below this one has a shard past both the top and the floor.
    bound = max(high, floor)
    least = total / bound if bound > 0 else math.inf
    start = min(limit, max(2, math.ceil(least))) if math.isfinite(least) else limit
    fit: tuple[int, list[Bin]] | None = None
    best: tuple[tuple[float, float], int, list[Bin]] = ((crowded(one), one[0].load), 1, one)
    for k in range(start, limit + 1):
        bins = pack(items, k, setup)
        loads = [b.load for b in bins]
        if max(loads) <= high:
            if min(loads) >= low:
                return k, bins
            fit = fit or (k, bins)
            if total / (k + 1) + setups < low:
                break
        elif fit is None and all(b.load <= high or len(b.items) == 1 for b in bins):
            return k, bins
        if (crowded(bins), max(loads)) < best[0]:
            best = ((crowded(bins), max(loads)), k, bins)
    if fit is not None:
        return fit
    return best[1], best[2]


def measured_load(b: Bin, setup: dict[str, float]) -> float:
    """B's projection without its estimates: its setups and the work measures priced."""
    return sum(setup[g] for g in b.groups) + sum(
        it.work if it.known is None else it.known for it in b.items
    )


def exempt(it: Item, setup: dict[str, float]) -> bool:
    """IT holds an estimate that passes BOUND_SECS while its measured part fits, so alone in a
    shard it is the job deadline's (breaches)."""
    alone = Bin(load=setup[it.group] + it.work, groups={it.group}, items=[it])
    return alone.load > BOUND_SECS and not breaches([alone], setup)


def breaches(bins: list[Bin], setup: dict[str, float], *, whole: bool = False) -> list[int]:
    """The indices of BINS that project past BOUND_SECS, but for a shard the job's deadline bounds.

    That is a shard of one item alone, or with WHOLE its items as one job no
    count splits, holding an estimate (PRIOR_ERROR times a prior) and whose
    measured part and setup fit BOUND_SECS: a job's deadline stops it in time to
    save what it measured, which prices it the next week.
    """
    return [
        i
        for i, b in enumerate(bins)
        if b.load > BOUND_SECS
        and not (
            (whole or len(b.items) == 1)
            and any(it.known is not None for it in b.items)
            and measured_load(b, setup) <= BOUND_SECS
        )
    ]


def deadline_bound(bins: list[Bin], setup: dict[str, float], *, whole: bool = False) -> list[int]:
    """The indices of BINS past BOUND_SECS that breaches() leaves to the job's deadline."""
    unsafe = set(breaches(bins, setup, whole=whole))
    return [i for i, b in enumerate(bins) if b.load > BOUND_SECS and i not in unsafe]


class UnsafePlanError(ValueError):
    """No plan keeps every job within BOUND_SECS (fit)."""


def fit(
    items: list[Item],
    setup: dict[str, float],
    top: float,
    limit: int = MAX_SHARDS,
    most: int = MATRIX_LIMIT,
) -> tuple[int, list[Bin]]:
    """choose()'s count and packing in at most LIMIT shards when no shard breaches the bound,
    else the fewest shards up to MOST in which none does; with a MOST of 1, the items are one job.

    Raises UnsafePlanError naming an item whose measured part and setup pass
    BOUND_SECS alone, which no count shortens, or when no count up to MOST
    keeps every shard within the bound.
    """
    whole = most <= 1
    over = [
        it
        for it in items
        if setup[it.group] + (it.work if it.known is None else it.known) > BOUND_SECS
    ]
    if over:
        it = max(over, key=lambda x: x.work if x.known is None else x.known)
        measured = it.work if it.known is None else it.known
        worst = f', its costliest measured mutant {it.worst}' if it.worst else ''
        raise UnsafePlanError(
            f'{it.unit or it.group} alone projects {(setup[it.group] + measured) / 60:.4g} min of '
            f'measured setup and work{worst}, past the {BOUND_SECS / 60:.0f} min a job may '
            f'project, and no cut or shard count shortens it'
        )
    k, bins = choose(items, setup, top, max(1, min(limit, most)))
    if not breaches(bins, setup, whole=whole):
        return k, bins
    if whole:
        raise UnsafePlanError(
            f'its one job projects {bins[0].load / 60:.4g} min, past the '
            f'{BOUND_SECS / 60:.0f} min a job may project, and it cannot be sharded'
        )
    # One shard per item is the most that helps: pack() then gives each item a shard of its own.
    hi = min(most, len(items))
    alone = [it for it in items if exempt(it, setup)]
    rest = [it for it in items if not exempt(it, setup)]
    least = len(alone) + math.ceil(
        (sum(it.work for it in rest) + sum(setup[g] for g in {it.group for it in rest}))
        / BOUND_SECS
    )
    for n in range(min(hi, max(k + 1, least)), hi + 1):
        bins = pack(items, n, setup)
        if not breaches(bins, setup):
            return n, bins
    raise UnsafePlanError(
        f'no count of up to {most} shards keeps every job within the {BOUND_SECS / 60:.0f} min a '
        f'job may project'
    )


def crowded(bins: list[Bin]) -> float:
    """The slowest of BINS that holds more than one item, which more shards could shorten; 0 for none."""
    return max((b.load for b in bins if len(b.items) > 1), default=0.0)


def capped(k: int, bins: list[Bin], items: list[Item], top: float, limit: int) -> bool:
    """Whether LIMIT, not the prices, set choose's K: more shards would have ended a shard of
    several items, past the top, sooner."""
    return k == limit < len(items) and crowded(bins) > top


def cap_reason(cap: int) -> str:
    """Why a plan whose prices wanted more runs CAP shards."""
    return f'a cap of {cap} shards'


def cap_note(where: str, plan: object) -> Template | None:
    """The warning a capped run plan carries, stating only what the plan holds, or None for any other."""
    if not isinstance(plan, dict) or not isinstance(plan.get('capped'), str):
        return None
    note = t'{where}: runs in {plan.get("shards")!r:.20} shard(s), capped by {plan["capped"]:.200}'
    longest = max((m for m in plan.get('projected_minutes') or [] if nonnegative(m)), default=None)
    top = plan.get('top_minutes')
    if longest is not None and nonnegative(top) and longest > top:
        note += t', so its longest sized shard projects {longest} min, past the {top:.1f} min the band allows'
    return note


def deadline_note(where: str, plan: object) -> Template | None:
    """The warning naming each unit a run plan runs alone under its job's deadline, or None."""
    alone = plan.get('deadline') if isinstance(plan, dict) else None
    if not isinstance(alone, list) or not alone or not all(isinstance(u, str) for u in alone):
        return None
    return (
        t'{where}: unmeasured, running alone under the deadline: {", ".join(alone):.2000} '
        t'(each estimated past the {BOUND_SECS / 60:.0f} min a job may project; its measures '
        t'price it next week)'
    )


def escape_data(s: str) -> str:
    """S as an annotation's message, escaped as the Actions toolkit's escapeData does."""
    return s.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def _plain(s: str) -> str:
    # The runner also reads `##[command]` anywhere in a line that is not a `::` command
    # (actions/runner ActionCommand.TryParse), and a plain line is never unescaped.
    return escape_data(s).replace('#', '%23')


def _code_span(s: str) -> str:
    s = escape_data(s)
    if not s:
        return ''
    fence = '`' * (max((len(r) for r in re.findall('`+', s)), default=0) + 1)
    pad = ' ' if s[0] in '` ' or s[-1] in '` ' else ''
    return f'{fence}{pad}{s}{pad}{fence}'


def render(msg: Template, quote: Callable[[str], str] = str) -> str:
    """MSG's text, each interpolated value passed through QUOTE and each literal part kept.

    A t-string's literal parts are what the script wrote and its values are
    data, so this is the one place target-written text gets its escaping.
    """
    if not isinstance(msg, Template):
        raise TypeError(f'a log line is a t-string, not {type(msg).__name__}')
    out = []
    for part in msg:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part.value, Template):
            out.append(render(part.value, quote))
        else:
            out.append(quote(format(convert(part.value, part.conversion), part.format_spec)))
    return ''.join(out)


def say(msg: Template) -> None:
    """Log MSG as a plain line: it starts with SAY_PREFIX, so no value it holds starts a workflow command."""
    print(SAY_PREFIX + render(msg, _plain), file=sys.stderr)


def annotate(kind: str, msg: Template, *, summary: bool = False) -> None:
    """Write MSG as a KIND annotation, and to the run summary too when SUMMARY."""
    if kind not in ('notice', 'warning', 'error'):
        raise ValueError(f'{kind!r} is not an annotation')
    print(f'::{kind}::{render(msg, escape_data)}', file=sys.stderr)
    if summary:
        summarize(msg)


def run_main(main: Callable[[], int]) -> int:
    """MAIN's exit code, with an uncaught error logged through say, so its text starts no command."""
    try:
        return main()
    except SystemExit as exc:
        if exc.code is None or isinstance(exc.code, int):
            return exc.code or 0
        annotate('error', t'{exc.code}')
        return 1
    except Exception:
        for line in traceback.format_exc().splitlines():
            say(t'{line}')
        return 1


def summarize(msg: Template, *, table: bool = False) -> None:
    """Append MSG to the job's run summary when GitHub Actions provides one, each value in a code span.

    MSG is a heading, or with TABLE a table row, whose values also escape '|'.
    """
    if path := os.environ.get('GITHUB_STEP_SUMMARY'):
        line = (
            render(msg, lambda v: _code_span(v).replace('|', '\\|'))
            if table
            else f'### {render(msg, _code_span)}'
        )
        with open(path, 'a') as f:
            f.write(line + '\n')


def attribute(
    events: Iterable[tuple[float, str, str]], start: float, workers: int
) -> dict[str, dict[str, list[float]]]:
    """Worker-seconds per mutant from one run's (time, file, mutant key) completions, as
    {file: {key: [seconds, ...]}}, one value per completion of that key in completion order.

    Each completion is charged the wall time since the one before it (or since
    START, the moment the workers began), times the workers. A completion
    stamped before the previous one is charged nothing.
    """
    out: dict[str, dict[str, list[float]]] = {}
    prev = start
    for t, path, key in events:
        gap = max(0.0, t - prev)
        prev = max(prev, t)
        out.setdefault(path, {}).setdefault(key, []).append(round(gap * workers, 3))
    return out


def count(v: object) -> bool:
    """An int from 0 to MAX_COUNT, never a bool."""
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_COUNT


def nonnegative(v: object) -> bool:
    """A finite number at or above 0, never a bool; an int only up to MAX_COUNT."""
    if isinstance(v, int):
        return count(v)
    return isinstance(v, float) and math.isfinite(v) and v >= 0


def known(probe: float, tick: float) -> float | None:
    """PROBE, or None when it is under TICK, its clock's resolution, and so measured nothing."""
    return probe if probe >= tick else None


def setup_price(measured: object, probe: float) -> float:
    """The setup a plan prices: MEASURED when it is a measure, else PROBE."""
    return float(measured) if nonnegative(measured) else probe


def mean_of(values: Iterable[object]) -> float | None:
    """The mean of the measures among VALUES, rounded to 0.1 s, or None for none."""
    kept = [float(v) for v in values if nonnegative(v)]
    return round(sum(kept) / len(kept), 1) if kept else None


def clean_secs(secs: object) -> dict[str, list[float]]:
    """The measures of an untrusted {mutant key: [seconds, ...]} map: each value that is a finite
    number at or above 0, and each key that keeps one."""
    if not isinstance(secs, dict):
        return {}
    out = {
        k: [float(x) for x in v if nonnegative(x)]
        for k, v in secs.items()
        if isinstance(k, str) and isinstance(v, list)
    }
    return {k: v for k, v in out.items() if v}


def clean_files(files: object) -> dict[str, dict]:
    """The {path: {blob, secs}} entries of FILES, a history's files record_fault accepted (or
    None for none), each with the measures clean_secs keeps and none without one."""
    if not isinstance(files, dict):
        return {}
    return {
        p: {'blob': e['blob'], 'secs': s} for p, e in files.items() if (s := clean_secs(e['secs']))
    }


def record_fault(rec: object) -> str:
    """Why REC is not a cost history ({files: {path: {blob, secs}}, setup_secs, prior_error}), or ''.

    Only its shape is judged: a value that is no measure is dropped where it is
    read (clean_secs), and a key no planned mutant holds prices nothing (pair).
    """
    if not isinstance(rec, dict) or not set(rec) <= {'files', 'setup_secs', 'prior_error'}:
        return 'it is not a {files, setup_secs, prior_error} object'
    files = rec.get('files', {})
    if not isinstance(files, dict) or not all(
        isinstance(e, dict)
        and set(e) == {'blob', 'secs'}
        and isinstance(e['blob'], str)
        and GIT_BLOB.fullmatch(e['blob'])
        and isinstance(e['secs'], dict)
        and all(isinstance(v, list) for v in e['secs'].values())
        for e in files.values()
    ):
        return 'its file entries are not all {blob, secs: {mutant: [seconds]}} measures'
    for key in ('setup_secs', 'prior_error'):
        if rec.get(key) is not None and not nonnegative(rec[key]):
            return f'its {key} {rec[key]!r:.200} is not a measure'
    return ''


def at_blobs(files: object, blobs: dict[str, str]) -> dict[str, dict[str, list[float]]]:
    """Each file's measures from history FILES taken at the git blob BLOBS gives it now; a file
    whose blob is missing on either side, or differs, has none."""
    return {p: e['secs'] for p, e in clean_files(files).items() if blobs.get(p) == e['blob']}


def pair(
    keys: Sequence[str], priors: Sequence[float | None], measured: dict[str, list[float]]
) -> list[tuple[float | None, float | None]]:
    """Each planned mutant's (prior, measure) for price, from its KEYS and PRIORS: the i-th
    mutant of one key takes MEASURED's i-th value of it, and a value no planned mutant takes,
    of an unknown key or past its count, prices nothing."""
    seen: dict[str, int] = {}
    out = []
    for key, prior in zip(keys, priors, strict=True):
        i = seen[key] = seen.get(key, -1) + 1
        values = measured.get(key) or []
        out.append((prior, values[i] if i < len(values) else None))
    return out


class Priced(NamedTuple):
    secs: float | None  # worker-seconds, None for an unknown cost
    measured: float  # the part of them measures set
    source: str  # 'measured', 'prior', 'partial' or 'unknown'
    worst: int | None  # the index of the costliest measured mutant, None for none


def price(mutants: Sequence[tuple[float | None, float | None]]) -> Priced:
    """A unit's worker-seconds for the next run: the one place either tool sets a price.

    MUTANTS holds, per mutant the unit (a file, range or module) runs, its tool's
    prior worker-seconds (None when its probe measured nothing) and its measure at
    the file's current blob (None for none). A measured mutant costs what it took,
    so a measure is never priced below itself, and any other PRIOR_ERROR times its
    prior. The source is 'measured' when every mutant is, 'prior' when none is,
    else 'partial'; with an unmeasured mutant of no prior the price is None and
    the source 'unknown', never 0.
    """
    got = [(m, i) for i, (_, m) in enumerate(mutants) if m is not None]
    measured = sum(m for m, _ in got)
    worst = max(got)[1] if got else None
    rest = [p for p, m in mutants if m is None]
    if any(p is None for p in rest):
        return Priced(None, measured, 'unknown', worst)
    source = 'measured' if not rest else 'prior' if len(got) == 0 else 'partial'
    return Priced(measured + sum(rest) * PRIOR_ERROR, measured, source, worst)


def totals(files: dict[str, dict[str, list[float]]]) -> tuple[float, int]:
    """The worker-seconds and value count of FILES' measures."""
    values = [v for secs in files.values() for vs in secs.values() for v in vs]
    return sum(values), len(values)


def merged(new: dict[str, list[float]], old: dict[str, list[float]]) -> dict[str, list[float]]:
    """One file's measures: NEW's values of each key, then OLD's past as many, so a cut sample
    keeps the last measure of each mutant it did not reach."""
    return {
        k: [*new.get(k, []), *old.get(k, [])[len(new.get(k, [])) :]]
        for k in sorted(set(new) | set(old))
    }


def attempt_mean(attempts: Sequence[dict[str, list[float]]]) -> dict[str, list[float]]:
    """One file's measures over several attempts at the same work: each mutant's mean over the
    attempts that measured it, so a cut attempt adds only the mutants it reached."""
    out = {}
    for k in sorted({k for a in attempts for k in a}):
        runs = [a[k] for a in attempts if k in a]
        out[k] = [
            round(sum(v[i] for v in runs if i < len(v)) / sum(1 for v in runs if i < len(v)), 3)
            for i in range(max(len(v) for v in runs))
        ]
    return out


def prior_error(rows: Iterable[tuple[object, float, float]]) -> float | None:
    """The largest ratio, over shards, of what a plan's prior-priced units took to their prior, or None.

    ROWS holds (shard, measured seconds, prior seconds) per unit the plan
    priced from its prior alone and this run measured whole. Summing per shard keeps a unit too small
    to move its shard from setting the ratio; a unit that fills its shard sets
    it whole.
    """
    acc: dict[object, list[float]] = {}
    for shard, measured, prior in rows:
        a = acc.setdefault(shard, [0.0, 0.0])
        a[0] += measured
        a[1] += prior
    ratios = [m / p for m, p in acc.values() if p > 0]
    return round(max(ratios), 2) if ratios else None


def note_prior_error(where: str, measured: float | None) -> None:
    """Log and summarize this week's measured prior error, warning when it is past PRIOR_ERROR."""
    if measured is None:
        return
    line = t'{where}: prior-priced work took up to {measured}x its prior in one shard this week'
    say(line)
    summarize(line)
    if measured > PRIOR_ERROR:
        annotate(
            'warning',
            t'{where}: prior-priced work took {measured}x its prior in one shard, past the '
            t'{PRIOR_ERROR}x mutation_cost.PRIOR_ERROR plans it at',
            summary=True,
        )


def load_json(path: Path, limit: int | None = MAX_INPUT_BYTES) -> object:
    """PATH's JSON value.

    Raises OSError when it is unreadable, ValueError when it is past LIMIT
    bytes (None for no limit), not UTF-8 or not JSON, and RecursionError when
    it nests deeper than the parser can follow.
    """
    with open(path, 'rb') as f:
        data = f.read() if limit is None else f.read(limit + 1)
    if limit is not None and len(data) > limit:
        raise ValueError(f'{path} is past {limit} bytes')
    return json.loads(data.decode())


def describe(exc: BaseException) -> str:
    """EXC for a warning line: repr escapes its newlines, so no text it carries starts a workflow command."""
    return repr(exc)[:300]


def refused(repo: str, dirs: Iterable[str | bytes]) -> list[dict]:
    """Each of DIRS that entry_key refuses for REPO, with the reason and its index in DIRS,
    each annotated as an error. A bytes name that is not UTF-8 is refused, shown escaped."""
    out = []
    for i, d in enumerate(dirs):
        try:
            if isinstance(d, bytes):
                try:
                    d = d.decode()
                except UnicodeDecodeError:
                    d = d.decode(errors='backslashreplace')
                    raise ValueError(f'directory {d!r} is not UTF-8') from None
            entry_key(repo, d)
        except ValueError as exc:
            annotate('error', t'{repo}: {str(exc)}, so it is refused')
            out.append({'dir': d, 'reason': str(exc), 'index': i})
    return out


def main() -> int:
    """`key --repo R [--dir D]`: print entry_key(R, D), D empty for the whole repository.

    `refused --repo R`: read NUL-terminated dirs on stdin and print refused()'s JSON list.
    """
    p = argparse.ArgumentParser(description=main.__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    k = sub.add_parser('key')
    k.add_argument('--repo', required=True)
    k.add_argument('--dir', default='')
    r = sub.add_parser('refused')
    r.add_argument('--repo', required=True)
    args = p.parse_args()
    if args.command == 'refused':
        dirs = sys.stdin.buffer.read().split(b'\0')
        dirs = dirs[:-1] if dirs and not dirs[-1] else dirs
        print(json.dumps(refused(args.repo, dirs), separators=(',', ':')))
        return 0
    try:
        print(entry_key(args.repo, args.dir or None))
    except ValueError as exc:
        annotate('error', t'{str(exc)}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(run_main(main))
