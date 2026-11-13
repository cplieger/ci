"""Per-file mutation cost from a recorded run, and the shard packing of both weekly mutation runs.

gremlins and Stryker each report one completion per mutant while a fixed pool of
workers runs the mutants in file order. A file's measured cost is the gap before
each of its completions times the workers (attribute). The next run prices a file
from that history while the file is unchanged, or from its tool's prior times
PRIOR_ERROR (project), and packs files into shards that each end near a target
(choose), at most MAX_SHARDS of them.
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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
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
# A unit with no measured history at its current git blob is planned at its tool's prior times
# this, a ruled margin: in Stryker run 43328238671 prior-priced work took at most 0.48x its prior
# per shard, and 3.53x in the largest single-file ratio (platform.ts, too small to move a shard);
# store.ts's 2.69x was against a price another file's ratio had already scaled down. Both weekly
# records measure it per shard (prior_error) and warn past this.
PRIOR_ERROR = 3.0
# A job matrix runs at most 256 jobs (https://docs.github.com/en/actions/reference/limits),
# which one call's run matrix keeps: MAX_SHARDS dirs of MAX_SHARDS shards (Stryker),
# or 3 attempts of MAX_SHARDS shards (gremlins).
MATRIX_LIMIT = 256
# deadset-ts's full Stryker run, about 36 machine-hours, needs about 16 to 19 shards
# at the band's top (5 to 31 min of setup). A forged price therefore holds at most
# 16 budget-length jobs of its run's concurrency group.
MAX_SHARDS = 16
# A measured cost per mutant more than this factor either way from its probe's is
# refused: run 42584241747's files measured 0.052 to 9.3 times their package's.
PRICE_FACTOR = 50
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
# A git object id, SHA-1 or SHA-256: the file version a Stryker cost history's range rows belong to.
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


def target_secs(setup: float) -> float:
    """The most work one shard of SETUP seconds' setup takes so a SLOWDOWN-times slower runner,
    which stretches the setup as well, ends by SAFE_JOB_MINUTES."""
    return max(0.0, SAFE_JOB_MINUTES * 60 / SLOWDOWN - setup)


# The band's top, setup and work together: a shard whose jobs average BAND over a projection
# this long holds setup + target_secs(setup) whatever its setup.
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


def crowded(bins: list[Bin]) -> float:
    """The slowest of BINS that holds more than one item, which more shards could shorten; 0 for none."""
    return max((b.load for b in bins if len(b.items) > 1), default=0.0)


def capped(
    k: int, bins: list[Bin], items: list[Item], setup: dict[str, float], top: float, limit: int
) -> bool:
    """Whether LIMIT, not the prices, set choose's K: more shards would have ended a shard of
    several items, past the top, sooner."""
    return k == limit < len(items) and crowded(bins) > top


def cap_reason(cap: int) -> str:
    """Why a plan whose prices wanted more runs CAP shards."""
    return f'a cap of {cap} shards'


def cap_note(where: str, plan: object) -> Template | None:
    """The warning a capped run plan carries, stating only what the plan holds, or None for any other.

    A plan's `unsized_shared` lists the modules and files of unknown cost that
    the cap put in a shard with other work.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get('capped'), str):
        return None
    note = t'{where}: runs in {plan.get("shards")!r:.20} shard(s), capped by {plan["capped"]:.200}'
    longest = max((m for m in plan.get('projected_minutes') or [] if nonnegative(m)), default=None)
    top = plan.get('top_minutes')
    if longest is not None and nonnegative(top) and longest > top:
        note += t', so its longest sized shard projects {longest} min, past the {top:.1f} min the band allows'
    shared = plan.get('unsized_shared')
    if isinstance(shared, list) and shared and all(isinstance(d, str) for d in shared):
        note += t'; unsized, planned as a full shard each, these share a shard: {", ".join(shared):.1000}'
    return note


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


def attribute(events: Iterable[tuple[float, str]], start: float, workers: int) -> dict[str, dict]:
    """Worker-seconds and completions per file from one run's (time, file) completions.

    Each completion is charged the wall time since the one before it (or since
    START, the moment the workers began), times the workers. A completion
    stamped before the previous one is charged nothing.
    """
    out: dict[str, dict] = {}
    prev = start
    for t, path in events:
        gap = max(0.0, t - prev)
        prev = max(prev, t)
        e = out.setdefault(path, {'secs': 0.0, 'mutants': 0})
        e['secs'] += gap * workers
        e['mutants'] += 1
    return {p: {'secs': round(e['secs'], 3), 'mutants': e['mutants']} for p, e in out.items()}


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


def plausible(measured: float, probe: float | None) -> bool:
    """MEASURED is within PRICE_FACTOR of PROBE either way; against an unknown PROBE, any positive measure."""
    if probe is None:
        return measured > 0
    return probe > 0 and probe / PRICE_FACTOR <= measured <= probe * PRICE_FACTOR


def setup_price(measured: object, probe: float) -> float:
    """The setup a plan prices: MEASURED when it is a plausible measure of PROBE, else PROBE."""
    usable = nonnegative(measured) and plausible(float(measured), known(probe, SECOND_TICK))
    return float(measured) if usable else probe


def clean_files(files: object) -> dict[str, dict]:
    """The usable {path: {secs, mutants}} rows of an untrusted cost record."""
    if not isinstance(files, dict):
        return {}
    return {
        p: {'secs': float(e['secs']), 'mutants': e['mutants']}
        for p, e in files.items()
        if isinstance(p, str)
        and isinstance(e, dict)
        and nonnegative(e.get('secs'))
        and count(e.get('mutants'))
        and e['mutants'] > 0
    }


def record_fault(rec: object) -> str:
    """Why REC is not a cost record ({files: {path: {secs, mutants}}, setup_secs}, Stryker's with
    blobs and prior_error), or ''."""
    if not isinstance(rec, dict) or not set(rec) <= {'files', 'setup_secs', 'blobs', 'prior_error'}:
        return 'it is not a {files, setup_secs, blobs, prior_error} object'
    if rec.get('prior_error') is not None and not nonnegative(rec['prior_error']):
        return f'its prior_error {rec["prior_error"]!r:.200} is not a ratio'
    blobs = rec.get('blobs', {})
    if not isinstance(blobs, dict) or not all(
        isinstance(v, str) and GIT_BLOB.fullmatch(v) for v in blobs.values()
    ):
        return 'its blobs are not all git object ids'
    files = rec.get('files', {})
    if (
        not isinstance(files, dict)
        or len(clean_files(files)) != len(files)
        or any(set(e) != {'secs', 'mutants'} for e in files.values())
    ):
        return 'its file rows are not all {secs, mutants} measures'
    if rec.get('setup_secs') is not None and not nonnegative(rec['setup_secs']):
        return f'its setup_secs {rec["setup_secs"]!r:.200} is not a measure'
    return ''


def at_blobs(
    history: dict | None, blobs: dict[str, str], file_of: Callable[[str], str] = str
) -> dict | None:
    """HISTORY with only the rows measured on the version BLOBS gives their file (FILE_OF a row's
    unit): a row whose file's blob is missing on either side, or differs, prices nothing now."""
    if not isinstance(history, dict):
        return history
    old = history.get('blobs') if isinstance(history.get('blobs'), dict) else {}
    return {
        **history,
        'files': {
            u: e
            for u, e in clean_files(history.get('files')).items()
            if blobs.get(file_of(u)) and old.get(file_of(u)) == blobs[file_of(u)]
        },
    }


def combine(records: list[dict]) -> dict:
    """One history from several runs of the same work: the mean per file and of the setup.

    Each record is {'files': {path: {secs, mutants}}, 'setup_secs': float}; a file
    or setup a record lacks is averaged over the records that have it.
    """
    acc: dict[str, list[dict]] = {}
    setups = []
    for r in records:
        for p, e in clean_files(r.get('files')).items():
            acc.setdefault(p, []).append(e)
        if nonnegative(r.get('setup_secs')):
            setups.append(float(r['setup_secs']))
    files = {
        p: {
            'secs': round(sum(e['secs'] for e in es) / len(es), 3),
            'mutants': round(sum(e['mutants'] for e in es) / len(es)),
        }
        for p, es in sorted(acc.items())
    }
    return {'files': files, 'setup_secs': round(sum(setups) / len(setups), 1) if setups else None}


def carry_forward(
    new: dict, old: object, keep: set[str] | None = None, planned: dict[str, int] | None = None
) -> dict:
    """NEW's history with OLD's last measure of each file NEW did not measure, limited to KEEP when given.

    A file a run never reached before its job stopped, or whose shard left no
    record, has no new measure, so its last one still prices it. A new sample
    of fewer than its PLANNED mutants, the ones a job finished before its budget
    cut it, prices nothing (price), so it replaces no last measure.
    """
    old = old if isinstance(old, dict) else {}
    fresh = {
        p: e
        for p, e in clean_files(new.get('files')).items()
        if planned is None or e['mutants'] >= planned.get(p, 0)
    }
    files = {**clean_files(old.get('files')), **fresh}
    setup = new.get('setup_secs') if nonnegative(new.get('setup_secs')) else old.get('setup_secs')
    return {
        'files': {p: e for p, e in sorted(files.items()) if keep is None or p in keep},
        'setup_secs': float(setup) if nonnegative(setup) else None,
    }


def price(n: int, prior: float | None, row: dict | None) -> tuple[float | None, str]:
    """A unit's worker-seconds for the next run and their source: the one place either tool sets a price.

    N is the mutants the unit (a file, range or module) now runs, PRIOR its
    tool's prior worker-seconds (None when its probe measured nothing), ROW its
    {secs, mutants} history at the unit's current blob, or None. ROW prices N at
    its cost per mutant ('measured') only when it measured at least N mutants
    and is plausible against PRIOR. Anything else costs PRIOR x PRIOR_ERROR,
    'refused' for an implausible ROW, else 'prior'; with no PRIOR it is
    (None, 'unknown'), never 0.
    """
    refused = False
    if row is not None:
        cost = row['secs'] / row['mutants']
        if n == 0 or plausible(cost, None if prior is None else prior / n):
            if row['mutants'] >= n:
                return n * cost, 'measured'
        else:
            refused = True
    if prior is None:
        return (None, 'unknown') if n else (0.0, 'prior')
    return prior * PRIOR_ERROR, 'refused' if refused else 'prior'


def spread(total: float, weights: list[float]) -> list[float]:
    """TOTAL shared over WEIGHTS in proportion, evenly when they sum to 0; the parts sum to TOTAL."""
    whole = sum(weights)
    return [total * w / whole if whole > 0 else total / len(weights) for w in weights]


def project(
    units: dict[str, tuple[int, float | None]], history: dict | None
) -> dict[str, tuple[float | None, str]]:
    """Each file's price for the next run (price); units: {path: (mutants it runs, prior)}."""
    hist = clean_files(history.get('files') if isinstance(history, dict) else None)
    return {path: price(n, p, hist.get(path)) for path, (n, p) in units.items()}


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


def refusals(priced: dict[str, tuple[float, str]]) -> int:
    """How many of project()'s files had their history refused."""
    return sum(1 for _, source in priced.values() if source == 'refused')


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
