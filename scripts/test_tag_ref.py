"""tag-ref.sh's tag_ref against a stub `gh` holding the repository's tags."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent

GH_STUB = r"""#!/usr/bin/env python3
import json, os, sys
state_path = os.environ['STUB_STATE']
state = json.load(open(state_path))
args = sys.argv[1:]
state['calls'].append(args)
def done(code, out='', err=''):
    json.dump(state, open(state_path, 'w'))
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)
if args[:1] == ['api'] and args[1].startswith('repos/o/app/git/ref/tags/'):
    tag = args[1].removeprefix('repos/o/app/git/ref/tags/')
    if tag in state['tags']:
        done(0, state['tags'][tag] + '\n')
    done(1, err='gh: Not Found (HTTP 404)\n')
if args[:2] == ['api', 'repos/o/app/git/refs']:
    fields = dict(a.split('=', 1) for a in args[2:] if '=' in a)
    tag, sha = fields['ref'].removeprefix('refs/tags/'), fields['sha']
    answer = state['creates'].pop(0) if state['creates'] else 'ok'
    if answer in ('ok', 'applied'):
        state['tags'][tag] = sha
    elif answer == 'elsewhere':
        state['tags'][tag] = 'b' * 40
    if answer == 'ok':
        done(0, '{}\n')
    done(1, err='gh: Reference update failed (HTTP 422)\n')
done(64, err=f'gh stub: unexpected {args}\n')
"""

COMMIT = 'c' * 40


class TagRef(unittest.TestCase):
    def run_tag_ref(self, tags=None, creates=()):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='tag-ref-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / 'gh').write_text(GH_STUB)
        (tmp / 'gh').chmod(0o755)
        state = tmp / 'state.json'
        state.write_text(json.dumps({'tags': tags or {}, 'creates': list(creates), 'calls': []}))
        script = (
            'set -euo pipefail\n'
            f'. "{SCRIPTS}/retry.sh"\n'
            f'. "{SCRIPTS}/tag-ref.sh"\n'
            'rc=0\n'
            f'tag_ref o/app v1.2.0 {COMMIT} || rc=$?\n'
            'echo "rc=${rc} created=${TAG_CREATED}"\n'
        )
        env = {
            **os.environ,
            'PATH': f'{tmp}:{os.environ["PATH"]}',
            'STUB_STATE': str(state),
            'READBACK_SLEEP': '0',
        }
        res = subprocess.run(
            ['bash', '-c', script], env=env, capture_output=True, text=True, check=False
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        doc = json.loads(state.read_text())
        creates = [c for c in doc['calls'] if c[:2] == ['api', 'repos/o/app/git/refs']]
        return res.stdout, doc['tags'], creates

    def test_an_absent_tag_is_created_at_the_commit(self):
        out, tags, creates = self.run_tag_ref()
        self.assertEqual(out, 'rc=0 created=true\n')
        self.assertEqual(tags, {'v1.2.0': COMMIT})
        self.assertEqual(
            creates,
            [['api', 'repos/o/app/git/refs', '-f', 'ref=refs/tags/v1.2.0', '-f', f'sha={COMMIT}']],
        )

    def test_a_tag_already_at_the_commit_is_kept_without_a_create(self):
        out, _, creates = self.run_tag_ref(tags={'v1.2.0': COMMIT})
        self.assertEqual(
            out,
            f'::notice::Tag v1.2.0 already exists at {COMMIT}, so it is reused\nrc=0 created=false\n',
        )
        self.assertEqual(creates, [])

    def test_a_tag_at_another_commit_is_refused_and_never_moved(self):
        out, tags, creates = self.run_tag_ref(tags={'v1.2.0': 'b' * 40})
        self.assertNotIn('so it is reused', out)
        self.assertIn(
            f'::error::Tag v1.2.0 exists at {"b" * 40}, not {COMMIT}, so it is not moved.', out
        )
        self.assertTrue(out.endswith('rc=1 created=false\n'), out)
        self.assertEqual((tags, creates), ({'v1.2.0': 'b' * 40}, []))

    def test_a_refused_create_is_read_again_and_retried(self):
        out, tags, creates = self.run_tag_ref(creates=['refused'])
        self.assertIn('::warning::tag_ref_once attempt 1/5 failed', out)
        self.assertTrue(out.endswith('rc=0 created=true\n'), out)
        self.assertEqual((tags, len(creates)), ({'v1.2.0': COMMIT}, 2))

    def test_a_refused_create_that_landed_is_kept_but_not_claimed(self):
        out, tags, creates = self.run_tag_ref(creates=['applied'])
        self.assertTrue(
            out.endswith(
                f'::notice::Tag v1.2.0 already exists at {COMMIT}, so it is reused\nrc=0 created=false\n'
            ),
            out,
        )
        self.assertEqual((tags, len(creates)), ({'v1.2.0': COMMIT}, 1))

    def test_a_refused_create_beside_another_commits_tag_is_refused(self):
        out, tags, creates = self.run_tag_ref(creates=['elsewhere'])
        self.assertTrue(out.endswith('rc=1 created=false\n'), out)
        self.assertEqual((tags, len(creates)), ({'v1.2.0': 'b' * 40}, 1))

    def test_a_create_refused_on_every_attempt_fails_after_five(self):
        out, tags, creates = self.run_tag_ref(creates=['refused'] * 5)
        self.assertIn('::error::tag_ref_once failed after 5 attempts', out)
        self.assertTrue(out.endswith('rc=2 created=false\n'), out)
        self.assertEqual((tags, len(creates)), ({}, 5))


if __name__ == '__main__':
    unittest.main()
