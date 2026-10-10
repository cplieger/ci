"""Bounded thread fan-out for independent per-target work, reported in input order.

`ordered(items, work, workers)` runs `work(item)` on at most `workers` threads and
yields `(item, result)` in input order. Whatever a call printed to `sys.stdout` is
written whole, in that same order, just before its pair is yielded, so concurrent
targets never interleave their log lines. A call that raised re-raises at its turn:
the calls not yet started are cancelled, the running ones finish, and the output of
every call that ran is written first. One worker runs the calls in input order.

Stdlib only.
"""

import io
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

WORKERS = 8
# The most a caller may ask for: every worker can have a request in flight, and GitHub's
# secondary limits start at 100 concurrent requests:
# https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#about-secondary-rate-limits
MAX_WORKERS = 16

_local = threading.local()


class _Router(io.TextIOBase):
    """`sys.stdout` while a fan-out runs: a worker thread writes to its own buffer,
    any other thread to the real stream."""

    def __init__(self, real):
        super().__init__()
        self.real = real

    def write(self, text):
        buf = getattr(_local, 'buf', None)
        return (self.real if buf is None else buf).write(text)

    def flush(self):
        if getattr(_local, 'buf', None) is None:
            self.real.flush()


def _captured(work, item):
    """(result, exception, printed text) of one call."""
    _local.buf = io.StringIO()
    try:
        return work(item), None, _local.buf.getvalue()
    except Exception as exc:
        return None, exc, _local.buf.getvalue()
    finally:
        _local.buf = None


def ordered(items, work, workers=WORKERS):
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f'workers must be 1 to {MAX_WORKERS}, not {workers}')
    items = list(items)
    real = sys.stdout
    sys.stdout = _Router(real)
    pool = ThreadPoolExecutor(max_workers=workers)

    def emit(text):
        real.write(text)
        real.flush()

    try:
        futures = [pool.submit(_captured, work, item) for item in items]
        for index, (item, future) in enumerate(zip(items, futures, strict=True)):
            result, exc, text = future.result()
            emit(text)
            if exc is not None:
                later = futures[index + 1 :]
                for pending in later:
                    pending.cancel()
                for ran in later:
                    if not ran.cancelled():
                        emit(ran.result()[2])
                raise exc
            yield item, result
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        sys.stdout = real
