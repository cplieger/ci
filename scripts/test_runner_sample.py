"""runner-sample.py, which mutation-stryker-repo.yaml's run job starts beside Stryker."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import yaml

SCRIPTS = pathlib.Path(__file__).resolve().parent
SAMPLER = SCRIPTS / 'runner-sample.py'
REPO_WORKFLOW = SCRIPTS.parent / '.github' / 'workflows' / 'mutation-stryker-repo.yaml'

_spec = importlib.util.spec_from_file_location('runner_sample', SAMPLER)
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

MEMINFO = (
    'MemTotal:       16384000 kB\n'
    'MemFree:         1000000 kB\n'
    'MemAvailable:    2048000 kB\n'
    'SwapTotal:       4096000 kB\n'
    'SwapFree:        3072000 kB\n'
)


def fake_proc(root: pathlib.Path) -> pathlib.Path:
    """A /proc with one Stryker, two of its workers, two Chromium processes and a shell."""
    proc = root / 'proc'
    (proc / 'pressure').mkdir(parents=True)
    (proc / 'meminfo').write_text(MEMINFO)
    (proc / 'loadavg').write_text('3.97 3.50 2.10 5/600 4242\n')
    (proc / 'vmstat').write_text('nr_free_pages 1\noom_kill 2\n')
    (proc / 'pressure' / 'memory').write_text(
        'some avg10=1.00 avg60=12.50 avg300=3.00 total=1\nfull avg10=0.00 avg60=4.25 avg300=1.00 total=1\n'
    )
    pages = 256 * 2**20 // os.sysconf('SC_PAGE_SIZE')
    for pid, comm, cmdline in (
        # Stryker's bin/stryker.js sets process.title, which rewrites comm and cmdline.
        (10, 'stryker', 'stryker\0'),
        (
            11,
            'node',
            'node\0/x/@stryker-mutator/core/dist/src/child-proxy/child-process-proxy-worker.js\0',
        ),
        (12, 'node-MainThread', 'node\0/x/child-process-proxy-worker.js\0'),
        (13, 'chrome', '/pw/chrome\0--headless\0'),
        (14, 'headless_shell', '/pw/headless_shell\0'),
        (15, 'bash', 'bash\0'),
    ):
        d = proc / str(pid)
        d.mkdir()
        (d / 'comm').write_text(comm + '\n')
        (d / 'cmdline').write_bytes(cmdline.encode())
        (d / 'statm').write_text(f'9999 {pages} 0 0 0 0 0\n')
    (proc / '16').mkdir()  # a process that ended between the listing and the read
    (proc / 'self').mkdir()
    return proc


class Sample(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.proc = fake_proc(self.tmp)

    def test_a_sample_holds_memory_swap_disk_pressure_and_the_mutation_processes(self):
        row = rs.sample(self.proc, self.tmp, self.tmp, 1794478475.6)
        self.assertEqual(
            {k: row[k] for k in rs.COLUMNS if k not in ('disk_avail_mb', 'tmp_avail_mb')},
            {
                'epoch': 1794478475,
                'mem_total_mb': 16000,
                'mem_avail_mb': 2000,
                'swap_total_mb': 4000,
                'swap_used_mb': 1000,
                'load1': '3.97',
                'psi_some60': '12.50',
                'psi_full60': '4.25',
                'oom_kills': '2',
                'procs': 6,
                'stryker': 1,
                'stryker_workers': 2,
                'node': 3,
                'node_rss_mb': 768,
                'chrome': 2,
                'chrome_rss_mb': 512,
            },
        )
        self.assertIsInstance(row['disk_avail_mb'], int)

    def test_a_kernel_without_psi_or_oom_counter_reads_as_unknown(self):
        (self.proc / 'pressure' / 'memory').unlink()
        (self.proc / 'vmstat').write_text('nr_free_pages 1\n')
        row = rs.sample(self.proc, self.tmp / 'gone', self.tmp, 0)
        self.assertEqual(
            (row['psi_some60'], row['psi_full60'], row['oom_kills'], row['disk_avail_mb']),
            ('-', '-', '-', '-'),
        )

    def test_each_sample_is_a_tsv_row_and_one_log_line_with_a_fixed_prefix(self):
        out = self.tmp / 'samples.tsv'
        proc = subprocess.run(
            [
                sys.executable,
                str(SAMPLER),
                '--out',
                str(out),
                '--interval',
                '0',
                '--count',
                '2',
                '--disk',
                str(self.tmp),
                '--proc',
                str(self.proc),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = out.read_text().splitlines()
        self.assertEqual(rows[0].split('\t'), list(rs.COLUMNS))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(len(r.split('\t')) == len(rs.COLUMNS) for r in rows))
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith('runner-sample epoch=') for line in lines), lines)
        self.assertIn(' mem_avail_mb=2000 ', lines[0])
        self.assertIn(' chrome=2 ', lines[0])

    def test_it_samples_until_stopped_and_stops_clean(self):
        out = self.tmp / 'samples.tsv'
        p = subprocess.Popen(
            [
                sys.executable,
                str(SAMPLER),
                '--out',
                str(out),
                '--interval',
                '60',
                '--disk',
                str(self.tmp),
                '--proc',
                str(self.proc),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 30
        while (
            time.monotonic() < deadline
            and len(out.read_text().splitlines() if out.exists() else []) < 2
        ):
            time.sleep(0.1)
        p.send_signal(signal.SIGTERM)
        stdout, stderr = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, stderr)
        self.assertEqual(len(out.read_text().splitlines()), 2)
        self.assertEqual(len(stdout.splitlines()), 1)

    def test_it_runs_on_this_host_s_proc(self):
        if not pathlib.Path('/proc/meminfo').exists():
            self.skipTest('no /proc on this host')
        row = rs.sample(pathlib.Path('/proc'), self.tmp, self.tmp, time.time())
        self.assertGreater(row['mem_total_mb'], 0)
        self.assertGreaterEqual(row['procs'], 1)


class Wiring(unittest.TestCase):
    steps = yaml.safe_load(REPO_WORKFLOW.read_text())['jobs']['run']['steps']

    def step(self, name: str) -> dict:
        return next(s for s in self.steps if s.get('name') == name)

    def test_every_shard_samples_beside_stryker_and_uploads_the_series(self):
        run = self.step('Run stryker')['run']
        self.assertLess(run.index('runner-sample.py'), run.index('stryker-run.sh'))
        self.assertIn('--out "${RUNNER_TEMP}/samples.tsv"', run)
        self.assertIn('trap \'kill "${sampler}" 2>/dev/null || true\' EXIT', run)
        up = self.step("Upload this shard's runner samples")
        self.assertEqual(up['if'], '${{ !cancelled() }}')
        self.assertEqual(
            (up['with']['name'], up['with']['path']),
            (
                'strykersamples-${{ matrix.key }}-${{ matrix.shard }}',
                '${{ runner.temp }}/samples.tsv',
            ),
        )
        names = [s.get('name') for s in self.steps]
        self.assertLess(
            names.index('Run stryker'), names.index("Upload this shard's runner samples")
        )

    def test_a_sampler_that_exits_first_does_not_fail_a_finished_shard(self):
        # GitHub runs a step with no shell: as `bash --noprofile --norc -eo pipefail {0}`.
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 'scripts').mkdir()
        (tmp / 'scripts' / 'runner-sample.py').write_text('raise SystemExit(0)\n')
        (tmp / 'scripts' / 'stryker-run.sh').write_text('sleep 0.5\nexit 0\n')
        body = tmp / 'step.sh'
        body.write_text(self.step('Run stryker')['run'])
        out = tmp / 'output'
        proc = subprocess.run(
            ['bash', '--noprofile', '--norc', '-eo', 'pipefail', str(body)],
            cwd=tmp,
            env={
                **os.environ,
                'GITHUB_WORKSPACE': str(tmp),
                'RUNNER_TEMP': str(tmp),
                'GITHUB_OUTPUT': str(out),
                'ENTRY': 'r:.',
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out.read_text(), 'rc=0\n')


if __name__ == '__main__':
    unittest.main()
