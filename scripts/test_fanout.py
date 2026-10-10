"""fanout.py: input order, whole per-call output, failure at its turn; and
ghrest's write pacing and shared rate-limit pause that the concurrent sync relies on."""

from __future__ import annotations

import contextlib
import io
import itertools
import subprocess
import sys
import threading
import time
import unittest

import fanout
import ghrest
from test_ghrest import reply


def captured(fn):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn()
    return result, out.getvalue()


class Ordered(unittest.TestCase):
    def test_results_and_output_follow_the_input_order_whatever_finishes_first(self):
        def work(n):
            print(f'{n} start')
            time.sleep(0.01 * (5 - n))
            print(f'{n} end')
            return n * n

        got, out = captured(lambda: list(fanout.ordered(range(5), work, 5)))
        self.assertEqual(got, [(n, n * n) for n in range(5)])
        self.assertEqual(out, ''.join(f'{n} start\n{n} end\n' for n in range(5)))

    def test_the_calls_really_overlap_and_never_exceed_the_bound(self):
        live, peak, lock = [0], [0], threading.Lock()

        def work(_n):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.02)
            with lock:
                live[0] -= 1

        list(fanout.ordered(range(12), work, 3))
        self.assertEqual(peak[0], 3)

    def test_one_worker_runs_the_calls_one_at_a_time_in_order(self):
        seen = []
        list(fanout.ordered(range(6), seen.append, 1))
        self.assertEqual(seen, list(range(6)))

    def test_a_raising_call_re_raises_at_its_turn_after_the_output_before_it(self):
        def work(n):
            print(f'{n}')
            if n == 2:
                raise ValueError('boom')
            return n

        got = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaisesRegex(ValueError, 'boom'):
            got.extend(fanout.ordered(range(4), work, 1))
        self.assertEqual(got, [(0, 0), (1, 1)])
        # The one worker may have started call 3 before the failure was seen.
        self.assertIn(out.getvalue(), ('0\n1\n2\n', '0\n1\n2\n3\n'))

    def test_a_raise_cancels_the_calls_not_started_and_flushes_the_running_ones(self):
        second_started = threading.Event()
        ran = []

        def work(n):
            if n == 0:
                second_started.wait(5)
                print('0')
                raise ValueError('boom')
            if n == 1:
                second_started.set()
                print('1')
                time.sleep(0.1)
                return n
            ran.append(n)
            print(n)
            time.sleep(0.2)
            return n

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaisesRegex(ValueError, 'boom'):
            list(fanout.ordered(range(8), work, 2))
        # The worker call 0 frees may take call 2 before the cancel lands.
        self.assertIn(ran, ([], [2]))
        self.assertEqual(out.getvalue(), '0\n1\n' + ''.join(f'{n}\n' for n in ran))

    def test_a_worker_count_outside_one_to_the_bound_is_refused(self):
        for workers in (0, fanout.MAX_WORKERS + 1):
            with self.assertRaises(ValueError):
                list(fanout.ordered([1], lambda n: n, workers))
        self.assertEqual(list(fanout.ordered([1], lambda n: n, fanout.MAX_WORKERS)), [(1, 1)])

    def test_stdout_is_restored_and_the_main_thread_prints_straight_through(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _item, _result in fanout.ordered([1], lambda _n: print('worker')):
                print('main')
            self.assertIs(sys.stdout, out)
        self.assertEqual(out.getvalue(), 'worker\nmain\n')


class Pacing(unittest.TestCase):
    def test_writes_start_a_gap_apart_and_reads_never_wait(self):
        clock = [100.0]
        slept = []

        def sleep(s):
            slept.append(round(s, 6))
            clock[0] += s

        pacer = ghrest.Pacer(1.0, sleep=sleep, clock=lambda: clock[0])
        calls = []
        run = ghrest.paced(
            lambda args, _stdin=None, _timeout=None: (
                calls.append(args) or subprocess.CompletedProcess(args, 0, b'', b'')
            ),
            pacer,
        )
        run(['api', '-i', '-X', 'POST', 'repos/x/pulls'])
        run(['api', '-i', 'repos/x/pulls'])
        run(['api', '-i', '-X', 'DELETE', 'repos/x/git/refs/heads/b'])
        run(['pr', 'merge', '1', '--auto'])
        self.assertEqual(slept, [1.0, 1.0])
        self.assertEqual(len(calls), 4)

    def test_writes_from_contending_threads_start_a_gap_apart(self):
        gap, n = 0.1, 6

        def slow_clock():
            # Yields between a writer's read of the schedule and its update, so an
            # unserialised pacer lets the writers through together.
            time.sleep(0.005)
            return time.monotonic()

        pacer = ghrest.Pacer(gap, clock=slow_clock)
        starts, lock = [], threading.Lock()

        def record(_args, _stdin=None, _timeout=None):
            with lock:
                starts.append(time.monotonic())

        run = ghrest.paced(record, pacer)
        barrier = threading.Barrier(n)

        def writer():
            barrier.wait()
            run(['api', '-i', '-X', 'POST', 'repos/x/pulls'])

        threads = [threading.Thread(target=writer) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        gaps = [b - a for a, b in itertools.pairwise(sorted(starts))]
        self.assertEqual(len(gaps), n - 1)
        self.assertGreaterEqual(min(gaps), gap * 0.7)

    def test_a_late_wake_still_leaves_the_next_writer_a_full_gap(self):
        clock = [0.0]
        slept = []
        late = [1.1]

        def sleep(s):
            slept.append(round(s, 6))
            clock[0] += s + (late.pop() if late else 0)

        pacer = ghrest.Pacer(1.0, sleep=sleep, clock=lambda: clock[0])
        for _ in range(3):
            pacer.wait()
        self.assertEqual(slept, [1.0, 1.0])

    def test_a_pause_holds_reads_and_writes_until_it_ends(self):
        clock = [100.0]
        slept = []

        def sleep(s):
            slept.append(round(s, 6))
            clock[0] += s

        pacer = ghrest.Pacer(0.0, sleep=sleep, clock=lambda: clock[0])
        pacer.pause(30)
        pacer.pause(10)
        pacer.wait(write=False)
        pacer.wait(write=True)
        self.assertEqual(slept, [30.0])

    def test_a_rate_limit_refusal_holds_every_client_sharing_the_pacer(self):
        clock = [100.0]

        def advance(s):
            clock[0] += s

        pacer = ghrest.Pacer(0.0, sleep=advance, clock=lambda: clock[0])
        replies = {
            'a': [reply(429, {'message': 'x'}, {'Retry-After': '60'}), reply(200, {})],
            'b': [reply(200, {})],
        }
        entered = []

        def run(args, _stdin=None, _timeout=None):
            entered.append((args[-1], clock[0]))
            return replies[args[-1]].pop(0)

        shared = ghrest.paced(run, pacer)
        b = ghrest.Client(run=shared, sleep=advance, now=lambda: 0, pause=pacer.pause)
        # Another worker's request lands while `a` sleeps out its refusal.
        a = ghrest.Client(run=shared, sleep=lambda _s: b.get('b'), now=lambda: 0, pause=pacer.pause)
        a.get('a')
        self.assertEqual(entered, [('a', 100.0), ('b', 160.0), ('a', 160.0)])

    def shared_clients(self, a_replies, **a_kw):
        """(a, b, entered): two Clients on one paced runner and fake clock at 100;
        `entered` records each request's path and start time."""
        clock = [100.0]

        def advance(s):
            clock[0] += s

        pacer = ghrest.Pacer(0.0, sleep=advance, clock=lambda: clock[0])
        replies = {'a': list(a_replies), 'b': [reply(200, {})]}
        entered = []

        def run(args, _stdin=None, _timeout=None):
            entered.append((args[-1], clock[0]))
            return replies[args[-1]].pop(0)

        shared = ghrest.paced(run, pacer)
        a = ghrest.Client(run=shared, sleep=advance, now=lambda: 0, pause=pacer.pause, **a_kw)
        b = ghrest.Client(run=shared, sleep=advance, now=lambda: 0, pause=pacer.pause)
        return a, b, entered

    def test_a_refusal_on_the_last_attempt_still_holds_the_other_clients(self):
        a, b, entered = self.shared_clients(
            [reply(429, {'message': 'x'}, {'Retry-After': '60'})], tries=1
        )
        with self.assertRaises(ghrest.ApiError):
            a.get('a')
        b.get('b')
        self.assertEqual(entered, [('a', 100.0), ('b', 160.0)])

    def test_a_cooldown_past_the_cap_fails_the_other_clients_without_a_request(self):
        a, b, entered = self.shared_clients([reply(429, {'message': 'x'}, {'Retry-After': '700'})])
        with self.assertRaises(ghrest.ApiError):
            a.get('a')
        with self.assertRaises(ghrest.ApiError) as caught:
            b.get('b')
        self.assertTrue(caught.exception.rate_limited)
        self.assertIn('past the 600s wait cap', str(caught.exception))
        self.assertEqual(entered, [('a', 100.0)])

    def test_a_rate_limited_command_holds_every_caller_then_is_retried(self):
        clock = [100.0]
        arriving = []

        def sleep(s):
            # Another worker's read arrives while the refused command waits.
            if arriving:
                arriving.pop()()
            clock[0] += s

        pacer = ghrest.Pacer(0.0, sleep=sleep, clock=lambda: clock[0])
        limited = subprocess.CompletedProcess([], 1, b'', b'GraphQL: API rate limit exceeded')
        answers = {'merge': [limited, subprocess.CompletedProcess([], 0, b'', b'')]}
        entered = []

        def run(args, _stdin=None, _timeout=None):
            entered.append((args[-1], clock[0]))
            return answers[args[-1]].pop(0) if args[0] == 'pr' else reply(200, {})

        shared = ghrest.paced(run, pacer)
        arriving.append(lambda: shared(['api', '-i', 'read']))
        self.assertEqual(shared(['pr', 'merge', 'merge']).returncode, 0)
        self.assertEqual([e[0] for e in entered], ['merge', 'read', 'merge'])
        self.assertEqual(entered[1][1], 100.0 + ghrest.SECONDARY_WAIT)
        self.assertGreaterEqual(entered[2][1], 100.0 + ghrest.SECONDARY_WAIT)

    def test_a_command_rate_limit_retry_waits_longer_each_time_and_only_a_limit_retries(self):
        limited = subprocess.CompletedProcess(
            [], 1, b'', b'You have exceeded a secondary rate limit'
        )
        refused = subprocess.CompletedProcess([], 1, b'', b'Pull request is in clean status')
        wait = ghrest.SECONDARY_WAIT
        for name, proc, args, starts, held_until in (
            ('a rate limit', limited, ['pr', 'merge', '1'], [0, wait, 3 * wait], 7 * wait),
            ('another refusal', refused, ['pr', 'merge', '1'], [0], 0.0),
            ('an api call', limited, ['api', '-i', '-X', 'PUT', 'x'], [0], 0.0),
        ):
            with self.subTest(name):
                clock, got = [0.0], []

                def advance(s, clock=clock):
                    clock[0] += s

                def run(a, _s=None, _t=None, got=got, proc=proc, clock=clock):
                    got.append(clock[0])
                    return proc

                pacer = ghrest.Pacer(0.0, sleep=advance, clock=lambda clock=clock: clock[0])
                shared = ghrest.paced(run, pacer)
                self.assertIs(shared(args), proc)
                self.assertEqual(got, starts)
                # The last refusal holds the next caller too.
                pacer.wait(write=False)
                self.assertEqual(clock[0], held_until)

    def test_each_command_starts_its_rate_limit_waits_from_a_minute(self):
        limited = subprocess.CompletedProcess([], 1, b'', b'API rate limit exceeded')
        done = subprocess.CompletedProcess([], 0, b'', b'')
        answers = [limited, done, limited, done]
        clock, pauses = [0.0], []

        def advance(s):
            clock[0] += s

        pacer = ghrest.Pacer(0.0, sleep=advance, clock=lambda: clock[0])
        hold = pacer.pause
        pacer.pause = lambda s: (pauses.append(s), hold(s))
        shared = ghrest.paced(lambda *_a: answers.pop(0), pacer)
        for _ in range(2):
            self.assertIs(shared(['pr', 'merge', '1']), done)
        self.assertEqual(pauses, [ghrest.SECONDARY_WAIT, ghrest.SECONDARY_WAIT])

    def test_what_a_command_rate_limit_refusal_is(self):
        def proc(rc, err):
            return subprocess.CompletedProcess([], rc, b'', err)

        self.assertTrue(ghrest.refused_by_rate_limit(proc(1, b'API rate limit exceeded for x')))
        self.assertTrue(ghrest.refused_by_rate_limit(proc(1, 'a secondary Rate Limit')))
        self.assertFalse(ghrest.refused_by_rate_limit(proc(1, b'not mergeable')))
        self.assertFalse(ghrest.refused_by_rate_limit(proc(0, b'rate limit')))

    def test_a_zero_gap_never_waits(self):
        slept = []
        pacer = ghrest.Pacer(0.0, sleep=slept.append, clock=lambda: 5.0)
        for _ in range(3):
            pacer.wait()
        self.assertEqual(slept, [])

    def test_what_counts_as_a_write(self):
        self.assertFalse(ghrest.is_write(['api', '-i', 'repos/x']))
        self.assertFalse(ghrest.is_write(['api', '-i', '-X', 'GET', 'repos/x']))
        self.assertTrue(ghrest.is_write(['api', '-i', '-X', 'PATCH', 'repos/x']))
        self.assertTrue(ghrest.is_write(['pr', 'merge', '1']))


if __name__ == '__main__':
    unittest.main()
