"""GitHub REST over `gh api -i`, with the one retry policy the scripts share.

Every outcome is read from the response `gh api -i` prints, on a non-2xx exit too:
- 2xx returns; 404 raises NotFoundError; any other status raises ApiError.
- A 429, or a 403 that is a rate limit, waits for `retry-after` or `x-ratelimit-reset`
  (a minute with neither) while the call's total wait stays within `max_wait`, then
  raises naming the UTC time the limit lifts. `GET /rate_limit` is never read: it
  misreports the GraphQL pool.
- A 5xx or a transport failure backs off 2, 4, 8 s for GET, PUT, PATCH and DELETE. A
  POST is retried only after a rate-limit refusal: a failed POST may have been applied.

Stdlib only; runs on the runner's system python3 (3.12).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import UTC, datetime
from typing import Any, NamedTuple

STATUS_LINE = re.compile(rb'HTTP/\d(?:\.\d)? (\d{3})')
IDEMPOTENT = frozenset({'GET', 'PUT', 'PATCH', 'DELETE'})
# GitHub's rule when neither retry-after nor an exhausted primary limit says how
# long: https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#exceeding-the-rate-limit
SECONDARY_WAIT = 60


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
        self, run=None, sleep=time.sleep, now=time.time, max_wait=600, tries=4, timeout=None
    ):
        self.run = run or run_process
        self.sleep, self.now = sleep, now
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
        waited, backoff = 0, 2
        for attempt in range(1, self.tries + 1):
            try:
                return self._once(method, path, args, stdin)
            except NotFoundError:
                raise
            except ApiError as err:
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
                    self.sleep(err.wait)
                elif method in IDEMPOTENT and (err.status is None or err.status >= 500):
                    self.sleep(backoff)
                    backoff *= 2
                else:
                    raise
        raise AssertionError('unreachable')

    def _once(self, method, path, args, stdin) -> Response:
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
            wait, reset = self._rate_wait(resp.headers)
            raise ApiError(
                resp.status,
                f'{where} rate limited until {utc(reset)}',
                reset,
                rate_limited=True,
                wait=wait,
            )
        raise ApiError(resp.status, f'{where} {detail}'.rstrip())

    def _rate_wait(self, headers) -> tuple[int, int]:
        """(seconds to wait, epoch second the limit lifts) from the refusal's headers."""
        now = int(self.now())
        after = headers.get('retry-after', '')
        if after.isdigit():
            return int(after), now + int(after)
        reset = headers.get('x-ratelimit-reset', '')
        if headers.get('x-ratelimit-remaining') == '0' and reset.isdigit():
            return max(int(reset) - now, 1), int(reset)
        return SECONDARY_WAIT, now + SECONDARY_WAIT

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
