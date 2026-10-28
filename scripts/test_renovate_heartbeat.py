"""Pin scripts/renovate_heartbeat.py's verdicts against canned search responses.

Run: python3 -m unittest discover -s scripts -p 'test_renovate_heartbeat.py'
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ghrest
import renovate_heartbeat

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
SINCE = '2026-10-26T09:00:00Z'
PR = {
    'number': 1392,
    'title': 'chore(deps): update renovate/renovate docker tag to v44.190.1 (dev)',
    'html_url': 'https://github.com/cplieger/docker-renovate-scheduler/pull/1392',
    'repository_url': 'https://api.github.com/repos/cplieger/docker-renovate-scheduler',
    'created_at': '2026-10-28T03:06:55Z',
}


def search(total, items=(), *, incomplete=False):
    return 200, {'total_count': total, 'incomplete_results': incomplete, 'items': list(items)}


class FakeGh:
    """Answers each `gh api -i` call with the next canned (status, body)."""

    def __init__(self, *answers):
        self.answers, self.queries = list(answers), []

    def __call__(self, args, stdin=None, timeout=None):
        url = urlsplit(args[-1])
        self.queries.append(parse_qs(url.query)['q'][0])
        status, body = self.answers.pop(0)
        out = f'HTTP/2.0 {status} X\r\ncontent-type: application/json\r\n\r\n{json.dumps(body)}'
        return subprocess.CompletedProcess(args, 0 if status < 400 else 1, out.encode(), b'')


class HeartbeatTest(unittest.TestCase):
    def run_main(self, gh, *extra):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.body, self.comment = tmp / 'body.md', tmp / 'comment.md'
        argv = [
            '--author',
            'app/tribble-trouble',
            '--owner',
            'cplieger',
            '--run-url',
            'https://example.invalid/run/1',
            '--body-file',
            str(self.body),
            '--comment-file',
            str(self.comment),
            *extra,
        ]
        client = ghrest.Client(run=gh, sleep=lambda _s: None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = renovate_heartbeat.main(argv, client=client, now=NOW)
        return code, out.getvalue().strip(), err.getvalue()

    def test_a_recent_pull_request_is_alive_and_words_the_closing_comment(self):
        gh = FakeGh(search(3, [PR]))
        code, verdict, _ = self.run_main(gh)
        self.assertEqual((code, verdict), (0, 'alive'))
        self.assertEqual(
            gh.queries,
            [f'is:pr author:app/tribble-trouble user:cplieger head:renovate created:>={SINCE}'],
        )
        comment = self.comment.read_text()
        self.assertIn('opened 3 pull requests in the last 48 hours', comment)
        self.assertIn('[cplieger/docker-renovate-scheduler#1392](', comment)
        self.assertFalse(self.body.exists())

    def test_a_count_without_items_is_still_alive(self):
        code, verdict, _ = self.run_main(FakeGh(search(2)))
        self.assertEqual((code, verdict), (0, 'alive'))
        self.assertIn(
            'opened 2 pull requests in the last 48 hours. Closing.', self.comment.read_text()
        )

    def test_an_empty_window_is_stalled_and_names_the_newest_pull_request(self):
        gh = FakeGh(search(0), search(812, [PR]))
        code, verdict, _ = self.run_main(gh)
        self.assertEqual((code, verdict), (0, 'stalled'))
        self.assertEqual(
            gh.queries[1], 'is:pr author:app/tribble-trouble user:cplieger head:renovate'
        )
        body = self.body.read_text()
        self.assertIn(f'in the last 48 hours, since {SINCE}', body)
        self.assertIn('Its newest pull request is [cplieger/docker-renovate-scheduler#1392](', body)
        self.assertIn('1. The container is running and healthy', body)
        self.assertIn('Run: https://example.invalid/run/1', body)
        self.assertNotIn('—', body, 'an em dash is refused in public issue text')
        self.assertFalse(self.comment.exists())

    def test_an_author_with_no_pull_request_at_all_is_stalled(self):
        code, verdict, _ = self.run_main(FakeGh(search(0), search(0)))
        self.assertEqual((code, verdict), (0, 'stalled'))
        self.assertIn('finds no pull request by it at all', self.body.read_text())

    def test_the_window_follows_hours(self):
        gh = FakeGh(search(1, [PR]))
        self.run_main(gh, '--hours', '24')
        self.assertTrue(gh.queries[0].endswith('created:>=2026-10-27T09:00:00Z'))

    def test_an_incomplete_empty_search_gives_no_verdict(self):
        code, verdict, err = self.run_main(FakeGh(search(0, incomplete=True)))
        self.assertEqual((code, verdict), (1, ''))
        self.assertIn('incomplete', err)
        self.assertFalse(self.body.exists())

    def test_an_incomplete_empty_lookup_of_the_newest_gives_no_verdict(self):
        code, verdict, err = self.run_main(FakeGh(search(0), search(0, incomplete=True)))
        self.assertEqual((code, verdict), (1, ''))
        self.assertIn('incomplete', err)
        self.assertFalse(self.body.exists())

    def test_a_failed_search_gives_no_verdict(self):
        gh = FakeGh(*[(500, {'message': 'boom'})] * 4)
        code, verdict, err = self.run_main(gh)
        self.assertEqual((code, verdict), (1, ''))
        self.assertIn('pull request search failed', err)
        self.assertFalse(self.body.exists())

    def test_hours_below_one_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_main(FakeGh(), '--hours', '0')
        self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
