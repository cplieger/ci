"""ghrest.py: the `gh api -i` parse and the retry policy, over an injected `run`."""

from __future__ import annotations

import json
import subprocess
import unittest

import ghrest

NOW = 1_800_000_000


def reply(status: int, body=b'', headers: dict | None = None) -> subprocess.CompletedProcess:
    """What `gh api -i` prints for one response: the status line ends in LF and
    each header in CRLF, as gh 2.96 writes them."""
    if not isinstance(body, bytes):
        body = json.dumps(body).encode()
    head = f'HTTP/2.0 {status} Reason\n'
    head += ''.join(f'{k}: {v}\r\n' for k, v in (headers or {}).items())
    rc = 0 if 200 <= status < 300 else 1
    return subprocess.CompletedProcess(['gh'], rc, (head + '\r\n').encode() + body, b'')


class Script:
    """A `run` that answers each call with the next reply (an exception is raised)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, args, stdin=None, timeout=None):
        self.calls.append((list(args), stdin, timeout))
        value = self.replies.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def client(*replies, **kw):
    sleeps = []
    run = Script(*replies)
    c = ghrest.Client(run=run, sleep=sleeps.append, now=lambda: NOW, **kw)
    return c, run, sleeps


class Parse(unittest.TestCase):
    def test_the_status_headers_and_body_are_read(self):
        resp = ghrest.parse(reply(200, {'a': 1}, {'X-RateLimit-Remaining': '7'}).stdout)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers['x-ratelimit-remaining'], '7')
        self.assertEqual(json.loads(resp.body), {'a': 1})

    def test_no_status_line_is_no_response(self):
        self.assertIsNone(ghrest.parse(b'error connecting to api.github.com\n'))


class Request(unittest.TestCase):
    def test_a_200_json_body_is_returned(self):
        c, run, sleeps = client(reply(200, {'name': 'ci'}))
        self.assertEqual(c.get('repos/cplieger/ci'), {'name': 'ci'})
        self.assertEqual(run.calls, [(['api', '-i', 'repos/cplieger/ci'], None, None)])
        self.assertEqual(sleeps, [])

    def test_a_404_raises_not_found_without_a_retry(self):
        c, run, sleeps = client(reply(404, {'message': 'Not Found'}))
        with self.assertRaises(ghrest.NotFoundError) as caught:
            c.get('repos/cplieger/x')
        self.assertEqual(caught.exception.status, 404)
        self.assertTrue(caught.exception.definitive)
        self.assertEqual((len(run.calls), sleeps), (1, []))
        c, _, _ = client(reply(404))
        self.assertIsNone(c.get_or_none('repos/cplieger/x'))

    def test_a_get_500_is_retried_after_two_seconds(self):
        c, run, sleeps = client(reply(500), reply(200, [1]))
        self.assertEqual(c.get('p'), [1])
        self.assertEqual((len(run.calls), sleeps), (2, [2]))

    def test_a_persistent_5xx_backs_off_2_4_8_then_raises(self):
        c, run, sleeps = client(*(reply(502) for _ in range(4)))
        with self.assertRaises(ghrest.ApiError) as caught:
            c.request('PATCH', 'p', {'x': 1})
        self.assertEqual((caught.exception.status, len(run.calls), sleeps), (502, 4, [2, 4, 8]))
        self.assertFalse(caught.exception.definitive)

    def test_a_post_5xx_is_not_retried(self):
        c, run, sleeps = client(reply(502), reply(201, {}))
        with self.assertRaises(ghrest.ApiError) as caught:
            c.send('POST', 'repos/cplieger/x/issues', {'title': 't'})
        self.assertEqual((caught.exception.status, len(run.calls), sleeps), (502, 1, []))

    def test_a_transport_failure_is_retried_on_get_and_not_on_post(self):
        lost = subprocess.CompletedProcess(['gh'], 1, b'', b'dial tcp: i/o timeout')
        c, run, sleeps = client(lost, reply(200, {}))
        self.assertEqual(c.get('p'), {})
        self.assertEqual(sleeps, [2])
        c, run, sleeps = client(lost, reply(201, {}))
        with self.assertRaises(ghrest.ApiError) as caught:
            c.send('POST', 'p', {})
        self.assertIsNone(caught.exception.status)
        self.assertIn('dial tcp: i/o timeout', str(caught.exception))
        self.assertEqual((len(run.calls), sleeps), (1, []))

    def test_a_timeout_on_every_attempt_raises_timeout(self):
        expired = subprocess.TimeoutExpired(['gh'], 10)
        c, run, sleeps = client(*(expired for _ in range(4)), timeout=10)
        with self.assertRaises(ghrest.RequestTimeoutError):
            c.get('p')
        self.assertEqual(run.calls[0][2], 10)
        self.assertEqual(sleeps, [2, 4, 8])

    def test_a_403_that_is_not_a_rate_limit_raises_at_once(self):
        c, run, sleeps = client(reply(403, {'message': 'Resource not accessible'}))
        with self.assertRaises(ghrest.ApiError) as caught:
            c.get('p')
        self.assertTrue(caught.exception.definitive)
        self.assertIn('HTTP 403 Resource not accessible', str(caught.exception))
        self.assertEqual((len(run.calls), sleeps), (1, []))

    def test_a_422_names_the_validation_codes(self):
        body = {'message': 'Validation Failed', 'errors': [{'code': 'already_exists'}]}
        c, _, _ = client(reply(422, body))
        with self.assertRaises(ghrest.ApiError) as caught:
            c.send('POST', 'repos/cplieger/x/labels', {'name': 'y'})
        self.assertIn('Validation Failed (already_exists)', str(caught.exception))

    def test_a_200_body_that_is_not_json_is_an_error_that_is_not_definitive(self):
        c, _, _ = client(reply(200, b'<html>maintenance</html>'))
        with self.assertRaises(ghrest.ApiError) as caught:
            c.get('p')
        self.assertFalse(caught.exception.definitive)

    def test_send_passes_the_json_body_on_stdin(self):
        c, run, _ = client(reply(200, {'number': 3}))
        self.assertEqual(c.send('PATCH', 'repos/cplieger/x/issues/3', {'body': 'b'}), {'number': 3})
        args, stdin, _ = run.calls[0]
        self.assertEqual(
            args, ['api', '-i', '-X', 'PATCH', 'repos/cplieger/x/issues/3', '--input', '-']
        )
        self.assertEqual(json.loads(stdin), {'body': 'b'})
        c, run, _ = client(reply(204))
        self.assertIsNone(c.send('DELETE', 'repos/cplieger/x/git/refs/heads/b'))
        self.assertEqual(run.calls[0][1], None)

    def test_headers_are_passed(self):
        c, run, _ = client(reply(200, b'\x00zip'))
        resp = c.request('GET', 'a/zip', headers=('Accept: application/octet-stream',))
        self.assertEqual(resp.body, b'\x00zip')
        self.assertEqual(
            run.calls[0][0], ['api', '-i', '-H', 'Accept: application/octet-stream', 'a/zip']
        )


class RateLimit(unittest.TestCase):
    def test_an_exhausted_primary_limit_waits_for_its_reset(self):
        limited = reply(
            403,
            {'message': 'API rate limit exceeded for user ID 1.'},
            {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(NOW + 30)},
        )
        c, run, sleeps = client(limited, reply(200, {}))
        self.assertEqual(c.get('p'), {})
        self.assertEqual((len(run.calls), sleeps), (2, [30]))

    def test_a_reset_beyond_the_cap_raises_naming_the_reset_time(self):
        reset = NOW + 3600
        limited = reply(
            403, {'message': 'x'}, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(reset)}
        )
        c, run, sleeps = client(limited, reply(200, {}))
        with self.assertRaises(ghrest.ApiError) as caught:
            c.get('p')
        self.assertIn(ghrest.utc(reset), str(caught.exception))
        self.assertIn('600s wait cap', str(caught.exception))
        self.assertEqual(caught.exception.reset, reset)
        self.assertFalse(caught.exception.definitive)
        self.assertEqual((len(run.calls), sleeps), (1, []))

    def test_retry_after_is_honoured_on_a_429(self):
        c, _, sleeps = client(
            reply(429, {'message': 'slow down'}, {'Retry-After': '5'}), reply(200, 1)
        )
        self.assertEqual(c.get('p'), 1)
        self.assertEqual(sleeps, [5])

    def test_a_secondary_limit_without_timing_headers_waits_a_minute(self):
        body = {'message': 'You have exceeded a secondary rate limit.'}
        c, _, sleeps = client(reply(403, body), reply(200, {}))
        self.assertEqual(c.get('p'), {})
        self.assertEqual(sleeps, [ghrest.SECONDARY_WAIT])

    def test_a_rate_limited_post_is_retried(self):
        limited = reply(403, {'message': 'x'}, {'Retry-After': '3'})
        c, run, sleeps = client(limited, reply(201, {'number': 9}))
        self.assertEqual(c.send('POST', 'repos/cplieger/x/issues', {}), {'number': 9})
        self.assertEqual((len(run.calls), sleeps), (2, [3]))

    def test_the_wait_cap_is_shared_by_every_retry_of_one_call(self):
        limited = reply(429, {'message': 'x'}, {'Retry-After': '400'})
        c, run, sleeps = client(limited, limited, reply(200, {}))
        with self.assertRaises(ghrest.ApiError):
            c.get('p')
        self.assertEqual((len(run.calls), sleeps), (2, [400]))


class Pages(unittest.TestCase):
    def test_a_short_page_ends_the_listing(self):
        c, run, _ = client(reply(200, list(range(100))), reply(200, [100, 101]))
        self.assertEqual(c.pages('user/repos?affiliation=owner'), list(range(102)))
        self.assertEqual(
            [a[-1] for a, _, _ in run.calls],
            [
                'user/repos?affiliation=owner&per_page=100&page=1',
                'user/repos?affiliation=owner&per_page=100&page=2',
            ],
        )

    def test_a_listing_longer_than_the_cap_raises(self):
        c, run, _ = client(*(reply(200, [0, 1]) for _ in range(3)))
        with self.assertRaises(ghrest.ApiError) as caught:
            c.pages('p', per_page=2, cap=2)
        self.assertIn('2 pages did not reach the end', str(caught.exception))
        self.assertEqual(len(run.calls), 2)

    def test_a_keyed_page_and_a_stop_predicate(self):
        c, run, _ = client(reply(200, {'check_runs': [1, 2, 3]}))
        self.assertEqual(c.pages('p', key='check_runs', stop=lambda n: n == 3), [1, 2])
        self.assertEqual(run.calls[0][0][-1], 'p?per_page=100&page=1')

    def test_a_page_that_is_not_a_list_raises(self):
        c, _, _ = client(reply(200, {'message': 'x'}))
        with self.assertRaises(ghrest.ApiError):
            c.pages('p')


if __name__ == '__main__':
    unittest.main()
