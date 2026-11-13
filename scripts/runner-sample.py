#!/usr/bin/env python3
"""Sample a runner's memory, swap, disk and mutation processes until stopped.

Each sample is one TSV row appended to --out (header first) and one `runner-sample`
log line, every --interval seconds, so a job whose runner is shut down still leaves
its series in the log; a runner that loses contact keeps no log. Stops cleanly on
SIGTERM or SIGINT.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

COLUMNS = (
    'epoch',
    'mem_total_mb',
    'mem_avail_mb',
    'swap_total_mb',
    'swap_used_mb',
    'disk_avail_mb',
    'tmp_avail_mb',
    'load1',
    'psi_some60',
    'psi_full60',
    'oom_kills',
    'procs',
    'stryker',
    'stryker_workers',
    'node',
    'node_rss_mb',
    'chrome',
    'chrome_rss_mb',
)
# Stryker forks every test runner and checker from this module.
WORKER = 'child-process-proxy-worker'
PAGE = os.sysconf('SC_PAGE_SIZE')


def meminfo(proc: Path) -> dict[str, int]:
    """/proc/meminfo's fields in kB."""
    out = {}
    for line in (proc / 'meminfo').read_text().splitlines():
        name, _, rest = line.partition(':')
        value = rest.split()
        if value and value[0].isdigit():
            out[name] = int(value[0])
    return out


def pressure(proc: Path) -> tuple[str, str]:
    """The memory PSI's some and full avg60, or '-' each on a kernel without PSI."""
    some = full = '-'
    try:
        lines = (proc / 'pressure' / 'memory').read_text().splitlines()
    except OSError:
        return some, full
    for line in lines:
        kind, *fields = line.split()
        avg = dict(f.split('=', 1) for f in fields if '=' in f).get('avg60', '-')
        if kind == 'some':
            some = avg
        elif kind == 'full':
            full = avg
    return some, full


def oom_kills(proc: Path) -> str:
    try:
        for line in (proc / 'vmstat').read_text().splitlines():
            name, _, value = line.partition(' ')
            if name == 'oom_kill':
                return value.strip()
    except OSError:
        pass  # no readable vmstat on this host: the sample reports '-'
    return '-'


def processes(proc: Path) -> dict[str, int]:
    """Counts and resident MiB of the Stryker, Node and Chromium processes."""
    n = {'procs': 0, 'stryker': 0, 'stryker_workers': 0, 'node': 0, 'chrome': 0}
    rss = {'node': 0, 'chrome': 0}
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            comm = (d / 'comm').read_text().strip()
            cmdline = (d / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            pages = int((d / 'statm').read_text().split()[1])
        except OSError, IndexError, ValueError:
            # The process ended between the listing and the read.
            continue
        n['procs'] += 1
        # Stryker's main process renames itself (bin/stryker.js sets process.title = 'stryker').
        kind = (
            'node'
            if comm.startswith('node') or comm == 'stryker'
            else 'chrome'
            if comm.startswith(('chrome', 'headless_shell'))
            else None
        )
        if kind is None:
            continue
        n[kind] += 1
        rss[kind] += pages * PAGE
        if kind == 'node' and WORKER in cmdline:
            n['stryker_workers'] += 1
        elif comm == 'stryker':
            n['stryker'] += 1
    return {**n, 'node_rss_mb': rss['node'] >> 20, 'chrome_rss_mb': rss['chrome'] >> 20}


def avail_mb(path: Path) -> int | str:
    try:
        st = os.statvfs(path)
    except OSError:
        return '-'
    return st.f_bavail * st.f_frsize >> 20


def sample(proc: Path, disk: Path, tmp: Path, now: float) -> dict[str, object]:
    mem = meminfo(proc)
    some, full = pressure(proc)
    return {
        'epoch': int(now),
        'mem_total_mb': mem.get('MemTotal', 0) >> 10,
        'mem_avail_mb': mem.get('MemAvailable', 0) >> 10,
        'swap_total_mb': mem.get('SwapTotal', 0) >> 10,
        'swap_used_mb': (mem.get('SwapTotal', 0) - mem.get('SwapFree', 0)) >> 10,
        'disk_avail_mb': avail_mb(disk),
        'tmp_avail_mb': avail_mb(tmp),
        'load1': (proc / 'loadavg').read_text().split()[0],
        'psi_some60': some,
        'psi_full60': full,
        'oom_kills': oom_kills(proc),
        **processes(proc),
    }


def log_line(row: dict[str, object]) -> str:
    return 'runner-sample ' + ' '.join(f'{k}={row[k]}' for k in COLUMNS)


def run(out: Path, interval: float, disk: Path, tmp: Path, proc: Path, count: int) -> None:
    """Append a sample to OUT every INTERVAL seconds, COUNT times (0 for until stopped)."""
    if not out.exists() or out.stat().st_size == 0:
        out.write_text('\t'.join(COLUMNS) + '\n')
    taken = 0
    while True:
        row = sample(proc, disk, tmp, time.time())
        with out.open('a') as f:
            f.write('\t'.join(str(row[k]) for k in COLUMNS) + '\n')
        print(log_line(row), flush=True)
        taken += 1
        if count and taken >= count:
            return
        time.sleep(interval)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--interval', type=float, default=60)
    p.add_argument('--disk', type=Path, default=Path.cwd(), help='the file system to report')
    p.add_argument('--count', type=int, default=0, help='stop after this many samples')
    p.add_argument('--proc', type=Path, default=Path('/proc'), help=argparse.SUPPRESS)
    args = p.parse_args()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))
    run(args.out, args.interval, args.disk, Path('/tmp'), args.proc, args.count)
    return 0


if __name__ == '__main__':
    sys.exit(main())
