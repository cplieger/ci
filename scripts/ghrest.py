"""GitHub REST over `gh api -i`, with the one retry policy the scripts share.

Every outcome is read from the response `gh api -i` prints, on a non-2xx exit too:
- 2xx returns; 404 raises NotFoundError; any other status raises ApiError.
- A 429, or a 403 that is a rate limit, waits for `retry-after` or `x-ratelimit-reset`
  (secondary_wait with neither) while the call's total wait stays within `max_wait`, then
  raises naming the UTC time the limit lifts. A Client given `pause` reports every
  rate-limit refusal there, one it gives up on too, so the threads sharing a Pacer
  hold as well. `GET /rate_limit` is never read: it misreports the GraphQL pool.
- A 5xx or a transport failure backs off 2, 4, 8 s for GET, PUT, PATCH and DELETE. A
  POST is retried only after a rate-limit refusal: a failed POST may have been applied.

Stdlib only; runs on the runner's system python3 (3.12).
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from datetime import UTC, datetime
from typing import Any, NamedTuple

STATUS_LINE = re.compile(rb'HTTP/\d(?:\.\d)? (\d{3})')
IDEMPOTENT = frozenset({'GET', 'PUT', 'PATCH', 'DELETE'})
# GitHub's rule when neither retry-after nor an exhausted primary limit says how
# long: https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#exceeding-the-rate-limit
SECONDARY_WAIT = 60


def secondary_wait(refusals: int) -> int:
    """Seconds to wait out a rate-limit refusal that names no time, after `refusals`
    earlier refusals of the same call: GitHub asks for an exponentially increasing wait
    while the limit persists:
    https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#handle-rate-limit-errors-appropriately
    """
    return SECONDARY_WAIT * 2**refusals


class ApiError(Exception):
    """A request that failed: `status` is the HTTP status, None when gh got no
    response; `reset` is the epoch second a rate limit that refused it lifts."""

    def __init__(self, status, message, reset=None, *, rate_limited=False, wait=0):
        super().__init__(message)
        self.status, self.reset, self.rate_limited, self.wait = status, reset, rate_limited, wait

    @property
    def definitive(self) -> bool:
        """Whether the failure is a fact about the resource (a 4xx that is not a
        rate limit) rather than a failure to learn about it."""
        return self.status is not None and 400 <= self.status < 500 and not self.rate_limited


class NotFoundError(ApiError):
    """HTTP 404."""


class RequestTimeoutError(ApiError):
    """gh did not answer within the client's timeout."""


class Response(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes


def run_process(args, stdin=None, timeout=None) -> subprocess.CompletedProcess:
    """`gh <args>` with bytes in and out; never raises on a non-zero exit."""
    return subprocess.run(
        ['gh', *args], input=stdin, capture_output=True, timeout=timeout, check=False
    )


def is_write(args) -> bool:
    """Whether a `gh <args>` call may write: an `api` call with a `-X` other than GET,
    or any command that is not `api` (`gh pr merge` is one)."""
    if args[:1] != ['api']:
        return True
    return '-X' in args and args[args.index('-X') + 1] != 'GET'


def refused_by_rate_limit(proc) -> bool:
    """Whether a failed non-API gh command (`gh pr merge`) was refused by a rate limit.
    gh prints GitHub's message but none of its headers, so the message is all there is."""
    err = proc.stderr if isinstance(proc.stderr, str) else (proc.stderr or b'').decode()
    return proc.returncode != 0 and 'rate limit' in err.lower()


class Pacer:
    """Admission for the gh calls of every thread sharing it: writes start at least
    `gap` seconds apart (0: no wait), and no call starts while a rate-limit `pause`
    runs. GitHub asks for a second between mutating requests, and for no requests
    until a limit lifts:
    https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api
    A call that would wait out more than `max_wait` of pause raises a rate-limited
    ApiError instead, sending nothing.
    """

    def __init__(self, gap=0.0, sleep=time.sleep, clock=time.monotonic, max_wait=600):
        self.gap, self.sleep, self.clock, self.max_wait = gap, sleep, clock, max_wait
        # Held across a write's sleep, so writers queue; reads never take it.
        self._writes = threading.Lock()
        self._lock = threading.Lock()
        self._next = 0.0
        self._resume = 0.0

    def pause(self, seconds) -> None:
        """Hold every call for `seconds` from now, or until a longer pause ends."""
        with self._lock:
            self._resume = max(self._resume, self.clock() + seconds)

    def _left(self, until) -> float:
        with self._lock:
            now = self.clock()
            held = self._resume - now
            if held > self.max_wait:
                raise ApiError(
                    None,
                    f'every call is rate limited until {utc(time.time() + held)}',
                    rate_limited=True,
                    wait=held,
                )
            return max(until, self._resume) - now

    def wait(self, write=True) -> None:
        if not write:
            while (left := self._left(0.0)) > 0:
                self.sleep(left)
            return
        with self._writes:
            while (left := self._left(self._next)) > 0:
                self.sleep(left)
            # From the clock, not the slot slept toward: a late wake still leaves
            # the next writer a full gap.
            self._next = self.clock() + self.gap


def paced(run, pacer: Pacer, tries=3):
    """`run` (run_process's contract) admitted by `pacer`, a write as a write; raises
    the pacer's ApiError. A non-API command refused by a rate limit pauses `pacer` for
    secondary_wait and is tried again, `tries` times in all; the last refusal is
    returned, its pause still published. A retry repeats no write: `gh pr merge`, the one
    command sent here, exits 0 on a pull request an earlier attempt already merged."""

    def call(args, stdin=None, timeout=None):
        for refusals in range(tries):
            pacer.wait(is_write(args))
            proc = run(args, stdin, timeout)
            if args[:1] == ['api'] or not refused_by_rate_limit(proc):
                return proc
            pacer.pause(secondary_wait(refusals))
        return proc

    return call


def parse(out: bytes) -> Response | None:
    """The response `gh api -i` printed, or None when it printed no status line."""
    m = STATUS_LINE.match(out)
    if not m:
        return None
    headers = {}
    _, _, rest = out.partition(b'\n')
    while rest:
        line, _, rest = rest.partition(b'\n')
        line = line.rstrip(b'\r')
        if not line:
            break
        name, _, value = line.partition(b':')
        headers[name.decode('latin-1').strip().lower()] = value.decode('latin-1').strip()
    return Response(int(m[1]), headers, rest)


def utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime('%Y-%m-%d %H:%M:%S UTC')


def error_detail(resp: Response) -> str:
    try:
        doc = json.loads(resp.body)
    except ValueError:
        return ' '.join(resp.body.decode('utf-8', 'replace').split())[:200]
    if not isinstance(doc, dict):
        return ''
    codes = [
        e.get('code') for e in doc.get('errors') or [] if isinstance(e, dict) and e.get('code')
    ]
    message = str(doc.get('message') or '')
    return f'{message} ({", ".join(codes)})' if codes else message


class Client:
    def __init__(
        self,
        run=None,
        sleep=time.sleep,
        now=time.time,
        max_wait=600,
        tries=4,
        timeout=None,
        pause=None,
    ):
        self.run = run or run_process
        self.sleep, self.now, self.pause = sleep, now, pause
        self.max_wait, self.tries, self.timeout = max_wait, tries, timeout

    def request(self, method: str, path: str, body=None, headers=()) -> Response:
        """The 2xx response to one request, after the retry policy above."""
        args = ['api', '-i']
        if method != 'GET':
            args += ['-X', method]
        for header in headers:
            args += ['-H', header]
        args.append(path)
        stdin = None
        if body is not None:
            args += ['--input', '-']
            stdin = json.dumps(body).encode()
        waited, backoff, refusals = 0, 2, 0
        for attempt in range(1, self.tries + 1):
            try:
                return self._once(method, path, args, stdin, refusals)
            except NotFoundError:
                raise
            except ApiError as err:
                # Before the raises below: a refusal this call gives up on still holds
                # the other threads until GitHub's cooldown ends.
                if err.rate_limited and self.pause is not None:
                    self.pause(err.wait)
                if attempt == self.tries:
                    raise
                if err.rate_limited:
                    if waited + err.wait > self.max_wait:
                        raise ApiError(
                            err.status,
                            f'{err}. That is past the {self.max_wait}s wait cap.',
                            err.reset,
                            rate_limited=True,
                        ) from None
                    waited += err.wait
                    refusals += 1
                    self.sleep(err.wait)
                elif method in IDEMPOTENT and (err.status is None or err.status >= 500):
                    self.sleep(backoff)
                    backoff *= 2
                else:
                    raise
        raise AssertionError('unreachable')

    def _once(self, method, path, args, stdin, refusals) -> Response:
        try:
            proc = self.run(args, stdin, self.timeout)
        except subprocess.TimeoutExpired:
            raise RequestTimeoutError(
                None, f'{method} {path}: gh timed out after {self.timeout}s'
            ) from None
        except OSError as err:
            raise ApiError(None, f'{method} {path}: gh did not run: {err}') from None
        out = proc.stdout if isinstance(proc.stdout, bytes) else (proc.stdout or '').encode()
        resp = parse(out)
        if resp is None:
            err = proc.stderr if isinstance(proc.stderr, str) else (proc.stderr or b'').decode()
            detail = ' '.join(err.split()) or f'exit {proc.returncode}'
            raise ApiError(None, f'{method} {path}: no HTTP response ({detail})')
        if 200 <= resp.status < 300:
            return resp
        where = f'{method} {path}: HTTP {resp.status}'
        detail = error_detail(resp)
        if resp.status == 404:
            raise NotFoundError(404, f'{where} {detail}'.rstrip())
        if resp.status == 429 or (
            resp.status == 403
            and (
                resp.headers.get('x-ratelimit-remaining') == '0'
                or 'retry-after' in resp.headers
                or 'rate limit' in detail.lower()
            )
        ):
            wait, reset = self._rate_wait(resp.headers, refusals)
            raise ApiError(
                resp.status,
                f'{where} rate limited until {utc(reset)}',
                reset,
                rate_limited=True,
                wait=wait,
            )
        raise ApiError(resp.status, f'{where} {detail}'.rstrip())

    def _rate_wait(self, headers, refusals) -> tuple[int, int]:
        """(seconds to wait, epoch second the limit lifts) from the refusal's headers;
        `refusals` is how many of this call's earlier attempts were refused."""
        now = int(self.now())
        after = headers.get('retry-after', '')
        if after.isdigit():
            return int(after), now + int(after)
        reset = headers.get('x-ratelimit-reset', '')
        if headers.get('x-ratelimit-remaining') == '0' and reset.isdigit():
            return max(int(reset) - now, 1), int(reset)
        wait = secondary_wait(refusals)
        return wait, now + wait

    def json_of(self, method: str, path: str, resp: Response) -> Any:
        if not resp.body.strip():
            return None
        try:
            return json.loads(resp.body)
        except ValueError:
            raise ApiError(resp.status, f'{method} {path}: response is not JSON') from None

    def get(self, path: str, headers=()) -> Any:
        return self.json_of('GET', path, self.request('GET', path, headers=headers))

    def get_or_none(self, path: str) -> Any:
        """get(), with a 404 read as None."""
        try:
            return self.get(path)
        except NotFoundError:
            return None

    def pages(self, path: str, key=None, per_page=100, cap=10, stop=None) -> list:
        """Every item of a paged listing (`key` names the list inside an object
        page), read until a short page or until `stop(item)` holds; ApiError when
        `cap` full pages did not reach the end."""
        out = []
        sep = '&' if '?' in path else '?'
        for page in range(1, cap + 1):
            url = f'{path}{sep}per_page={per_page}&page={page}'
            body = self.get(url)
            batch = body.get(key) if key and isinstance(body, dict) else body
            if not isinstance(batch, list):
                raise ApiError(None, f'GET {url}: the page is not a list')
            for item in batch:
                if stop and stop(item):
                    return out
                out.append(item)
            if len(batch) < per_page:
                return out
        raise ApiError(None, f'{path}: {cap} pages did not reach the end of the listing')

    def send(self, method: str, path: str, body=None) -> Any:
        """The JSON a write answered, None for an empty body."""
        return self.json_of(method, path, self.request(method, path, body))


DEFAULT = Client()
get, get_or_none, pages, send = DEFAULT.get, DEFAULT.get_or_none, DEFAULT.pages, DEFAULT.send
