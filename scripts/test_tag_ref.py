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
if args[:1] == ['api'] and args[1] == 'repos/o/app/git/ref/heads/dev':
    if state.get('head'):
        done(0, state['head'] + '\n')
    done(1, err='gh: Bad credentials (HTTP 401)\n')
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


class RunsTagRef:
    """Runs one tag-ref.sh call against the stub; head_reads holds its branch-head reads."""

    head_reads: list

    def run_tag_ref(self, tags=None, creates=(), head=None, call='tag_ref o/app v1.2.0', tail=''):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='tag-ref-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / 'gh').write_text(GH_STUB)
        (tmp / 'gh').chmod(0o755)
        state = tmp / 'state.json'
        state.write_text(
            json.dumps({'tags': tags or {}, 'creates': list(creates), 'calls': [], 'head': head})
        )
        script = (
            'set -euo pipefail\n'
            f'. "{SCRIPTS}/retry.sh"\n'
            f'. "{SCRIPTS}/tag-ref.sh"\n'
            'rc=0\n'
            f'{call} {COMMIT}{tail} || rc=$?\n'
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
        self.head_reads = [c for c in doc['calls'] if any('git/ref/heads/' in a for a in c)]
        return res.stdout, doc['tags'], creates


class TagRef(RunsTagRef, unittest.TestCase):
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


HANDOFF = 'tag_ref_or_handoff o/app dev v1.2.0'


class TagRefOrHandoff(RunsTagRef, unittest.TestCase):
    """A dev run whose branch moved past it loses ref-create (a workflow-file push)."""

    def test_a_create_that_succeeds_needs_no_head_read(self):
        out, tags, _ = self.run_tag_ref(call=HANDOFF, head='d' * 40)
        self.assertTrue(out.endswith('rc=0 created=true\n'), out)
        self.assertEqual((tags, self.head_reads), ({'v1.2.0': COMMIT}, []))

    def test_a_failed_create_after_the_branch_moved_hands_off(self):
        out, tags, _ = self.run_tag_ref(creates=['refused'] * 5, head='d' * 40, call=HANDOFF)
        self.assertIn(
            f'::warning::tag v1.2.0 not created: dev advanced past {COMMIT} (now {"d" * 40})', out
        )
        self.assertNotIn('::error::tag create', out)
        self.assertTrue(out.endswith('rc=2 created=false\n'), out)
        self.assertEqual(tags, {})

    def test_a_failed_create_still_at_the_head_is_refused(self):
        out, _, _ = self.run_tag_ref(creates=['refused'] * 5, head=COMMIT, call=HANDOFF)
        self.assertIn(
            "::error::tag create for v1.2.0 failed while this run is still dev's head", out
        )
        self.assertTrue(out.endswith('rc=1 created=false\n'), out)

    def test_a_failed_create_with_an_unreadable_head_is_refused(self):
        out, _, _ = self.run_tag_ref(creates=['refused'] * 5, call=HANDOFF)
        self.assertIn("::error::tag create for v1.2.0 failed and dev's head is unreadable", out)
        self.assertTrue(out.endswith('rc=1 created=false\n'), out)

    def test_a_tag_at_another_commit_is_refused_without_a_head_read(self):
        out, _, _ = self.run_tag_ref(tags={'v1.2.0': 'b' * 40}, head='d' * 40, call=HANDOFF)
        self.assertTrue(out.endswith('rc=1 created=false\n'), out)
        self.assertNotIn('advanced past', out)
        self.assertEqual(self.head_reads, [])


RUN = 'e' * 40


class TagRefOrHandoffFromRunHead(RunsTagRef, unittest.TestCase):
    """A renumber run tags an older build: the branch is compared with the run's own head."""

    def test_a_failed_create_after_the_branch_moved_past_the_run_hands_off(self):
        out, tags, _ = self.run_tag_ref(
            creates=['refused'] * 5, head='d' * 40, call=HANDOFF, tail=f' {RUN}'
        )
        self.assertIn(
            f'::warning::tag v1.2.0 not created: dev advanced past {RUN} (now {"d" * 40})', out
        )
        self.assertTrue(out.endswith('rc=2 created=false\n'), out)
        self.assertEqual(tags, {})

    def test_a_failed_create_while_the_run_is_the_head_is_refused(self):
        # The tagged commit is not the head, yet the run is: no superseded run.
        out, _, _ = self.run_tag_ref(
            creates=['refused'] * 5, head=RUN, call=HANDOFF, tail=f' {RUN}'
        )
        self.assertIn(
            "::error::tag create for v1.2.0 failed while this run is still dev's head", out
        )
        self.assertTrue(out.endswith('rc=1 created=false\n'), out)

    def test_an_empty_run_head_cannot_confirm_a_handoff(self):
        out, _, _ = self.run_tag_ref(
            creates=['refused'] * 5, head='d' * 40, call=HANDOFF, tail=" ''"
        )
        self.assertIn("::error::tag create for v1.2.0 failed and dev's head is unreadable", out)
        self.assertTrue(out.endswith('rc=1 created=false\n'), out)
        self.assertEqual(self.head_reads, [])


class BranchMovedPast(unittest.TestCase):
    def run_moved(self, head):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='moved-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / 'gh').write_text(GH_STUB)
        (tmp / 'gh').chmod(0o755)
        state = tmp / 'state.json'
        state.write_text(json.dumps({'tags': {}, 'creates': [], 'calls': [], 'head': head}))
        script = (
            'set -euo pipefail\n'
            f'. "{SCRIPTS}/retry.sh"\n'
            f'. "{SCRIPTS}/tag-ref.sh"\n'
            'rc=0\n'
            f'at=$(branch_moved_past o/app dev {COMMIT}) || rc=$?\n'
            'echo "rc=${rc} at=${at}"\n'
        )
        env = {**os.environ, 'PATH': f'{tmp}:{os.environ["PATH"]}', 'STUB_STATE': str(state)}
        res = subprocess.run(
            ['bash', '-c', script], env=env, capture_output=True, text=True, check=False
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_moved(self):
        self.assertEqual(self.run_moved('d' * 40), f'rc=0 at={"d" * 40}\n')

    def test_still_at_head(self):
        self.assertEqual(self.run_moved(COMMIT), f'rc=1 at={COMMIT}\n')

    def test_unreadable(self):
        self.assertEqual(self.run_moved(None), 'rc=2 at=\n')


if __name__ == '__main__':
    unittest.main()
