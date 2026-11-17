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
    if state.get('read_fail'):
        done(1, err='gh: Server Error (HTTP 502)\n')
    if tag in state['tags']:
        done(0, state['tags'][tag] + '\n')
    done(1, err='gh: Not Found (HTTP 404)\n')
if args[:1] == ['api'] and args[1] == 'repos/o/app/git/ref/heads/dev':
    if state.get('head'):
        done(0, state['head'] + '\n')
    done(1, err='gh: Bad credentials (HTTP 401)\n')
if args[:4] == ['api', '-X', 'POST', f'repos/o/app/statuses/{args[3].rsplit("/", 1)[-1]}']:
    if state.get('status_fail'):
        done(1, err='gh: Server Error (HTTP 502)\n')
    done(0, '{}\n')
if args[:3] == ['api', '-X', 'DELETE'] and args[3].startswith('repos/o/app/git/refs/tags/'):
    state['tags'].pop(args[3].removeprefix('repos/o/app/git/refs/tags/'), None)
    done(0)
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

    def run_tag_ref(
        self, tags=None, creates=(), head=None, call='tag_ref o/app v1.2.0', status_fail=False
    ):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='tag-ref-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / 'gh').write_text(GH_STUB)
        (tmp / 'gh').chmod(0o755)
        state = tmp / 'state.json'
        state.write_text(
            json.dumps(
                {
                    'tags': tags or {},
                    'creates': list(creates),
                    'calls': [],
                    'head': head,
                    'status_fail': status_fail,
                }
            )
        )
        script = (
            'set -euo pipefail\n'
            f'. "{SCRIPTS}/retry.sh"\n'
            f'. "{SCRIPTS}/tag-ref.sh"\n'
            'rc=0\n'
            f'{call} {COMMIT} || rc=$?\n'
            'echo "rc=${rc} created=${TAG_CREATED}"\n'
        )
        env = {
            **os.environ,
            'PATH': f'{tmp}:{os.environ["PATH"]}',
            'STUB_STATE': str(state),
            'READBACK_SLEEP': '0',
            'GITHUB_SERVER_URL': 'https://gh',
            'GITHUB_RUN_ID': '7',
        }
        res = subprocess.run(
            ['bash', '-c', script], env=env, capture_output=True, text=True, check=False
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        doc = json.loads(state.read_text())
        creates = [c for c in doc['calls'] if c[:2] == ['api', 'repos/o/app/git/refs']]
        self.head_reads = [c for c in doc['calls'] if any('git/ref/heads/' in a for a in c)]
        self.posts = [c for c in doc['calls'] if c[:3] == ['api', '-X', 'POST']]
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
            f'::error::Tag v1.2.0 exists at {"b" * 40}, not {COMMIT}. A published version never moves, so nothing is published under it.',
            out,
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

    def test_a_handoff_records_on_its_commit_the_head_that_releases_it(self):
        out, _, _ = self.run_tag_ref(creates=['refused'] * 5, head='d' * 40, call=HANDOFF)
        self.assertTrue(out.endswith('rc=2 created=false\n'), out)
        self.assertEqual(len(self.posts), 1, self.posts)
        post = self.posts[0]
        fields = dict(a.split('=', 1) for a in post[4:] if '=' in a)
        self.assertEqual(post[3], f'repos/o/app/statuses/{COMMIT}')
        self.assertEqual(
            (fields['state'], fields['context'], fields['target_url']),
            ('success', 'release/handoff/v1.2.0', 'https://gh/o/app/actions/runs/7'),
        )
        self.assertIn('d' * 40, fields['description'])
        self.assertLessEqual(len(fields['description']), 140)

    def test_a_handoff_whose_record_fails_still_hands_off_and_says_so(self):
        out, tags, _ = self.run_tag_ref(
            creates=['refused'] * 5, head='d' * 40, call=HANDOFF, status_fail=True
        )
        self.assertTrue(out.endswith('rc=2 created=false\n'), out)
        self.assertIn('::warning::handoff record for v1.2.0 not posted on', out)
        self.assertEqual(tags, {})

    def test_a_refusal_records_no_handoff(self):
        self.run_tag_ref(creates=['refused'] * 5, head=COMMIT, call=HANDOFF)
        self.assertEqual(self.posts, [])

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


class ClaimUnderALaterDevTag(unittest.TestCase):
    """claim_version and root_handed_off on one checkout: dev holds A then B, the run is at A."""

    def run_claim(self, tags_at_b=(), tags_at_a=(), version='v1.0.0-dev.2', claim_rc=0):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='claim-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = tmp / 'repo'
        git = ['git', '-C', str(repo), '-c', 'user.name=p', '-c', 'user.email=p@p']
        git += ['-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false']
        subprocess.run(['git', 'init', '-q', '-b', 'dev', str(repo)], check=True)
        shas = []
        for msg in ('base', 'A', 'B'):
            subprocess.run([*git, 'commit', '-q', '--allow-empty', '-m', msg], check=True)
            head = subprocess.run([*git, 'rev-parse', 'HEAD'], capture_output=True, text=True)
            shas.append(head.stdout.strip())
        a, b = shas[1], shas[2]
        subprocess.run([*git, 'update-ref', 'refs/remotes/origin/dev', b], check=True)
        for tag in tags_at_b:
            subprocess.run([*git, 'tag', tag, b], check=True)
        for tag in tags_at_a:
            subprocess.run([*git, 'tag', tag, a], check=True)
        subprocess.run([*git, 'checkout', '-q', '--detach', a], check=True)
        (tmp / 'gh').write_text(GH_STUB)
        (tmp / 'gh').chmod(0o755)
        state = tmp / 'state.json'
        tags = dict.fromkeys(tags_at_a, a) | dict.fromkeys(tags_at_b, b)
        doc = {'tags': tags, 'creates': [], 'calls': [], 'head': b}
        state.write_text(json.dumps(doc))
        # The later job's checkout holds the tag the claim created.
        script = (
            'set -euo pipefail\n'
            f'. "{SCRIPTS}/retry.sh"\n'
            f'. "{SCRIPTS}/tag-ref.sh"\n'
            f'claim_version o/app dev {version} {a} dev || exit 3\n'
            f'[ "$TAG_CREATED" != true ] || git tag {version} {a}\n'
            'rc=0\n'
            f'root_handed_off o/app dev {version} {a} dev >/dev/null || rc=$?\n'
            'echo "claimed=${CLAIMED} handed_off_rc=${rc}"\n'
        )
        env = {
            **os.environ,
            'PATH': f'{tmp}:{os.environ["PATH"]}',
            'STUB_STATE': str(state),
            'READBACK_SLEEP': '0',
            'GITHUB_OUTPUT': str(tmp / 'out'),
            'GIT_CONFIG_GLOBAL': '/dev/null',
        }
        res = subprocess.run(
            ['bash', '-c', script], cwd=repo, env=env, capture_output=True, text=True, check=False
        )
        self.assertEqual(res.returncode, 3 if claim_rc else 0, res.stdout + res.stderr)
        self.log = res.stdout + res.stderr
        calls = json.loads(state.read_text())['calls']
        self.creates = [c for c in calls if c[:2] == ['api', 'repos/o/app/git/refs']]
        self.posts = [c for c in calls if c[:3] == ['api', '-X', 'POST']]
        out = tmp / 'out'
        self.output = out.read_text() if out.exists() else ''
        return (res.stdout.splitlines() or [''])[-1], a, b

    def handoff_descriptions(self, version):
        return [
            dict(a.split('=', 1) for a in p[4:] if '=' in a)['description']
            for p in self.posts
            if f'context=release/handoff/{version}' in p
        ]

    def test_an_untagged_version_under_a_later_dev_tag_hands_off_before_any_create(self):
        result, _, b = self.run_claim(tags_at_b=['v1.1.0-dev.1'])
        self.assertEqual(result, 'claimed=false handed_off_rc=0')
        self.assertEqual((self.creates, self.output), ([], 'claimed=false\n'))
        self.assertEqual(
            self.handoff_descriptions('v1.0.0-dev.2'),
            [f"v1.1.0-dev.1 at {b} releases this commit's changes"],
        )

    def test_a_reused_claim_under_a_later_dev_tag_hands_off_with_the_root_readers(self):
        result, _, _ = self.run_claim(tags_at_b=['v1.0.0-dev.3'], tags_at_a=['v1.0.0-dev.2'])
        self.assertEqual(result, 'claimed=false handed_off_rc=0')
        self.assertEqual(self.creates, [])

    def test_with_no_later_dev_tag_the_claim_tags_and_the_root_readers_publish(self):
        result, a, _ = self.run_claim()
        self.assertEqual(result, 'claimed=true handed_off_rc=1')
        self.assertEqual(
            self.creates,
            [['api', 'repos/o/app/git/refs', '-f', 'ref=refs/tags/v1.0.0-dev.2', '-f', f'sha={a}']],
        )
        self.assertEqual(self.posts, [])

    def test_a_lane_claim_hands_off_to_its_own_lanes_later_dev_tag(self):
        result, _, b = self.run_claim(tags_at_b=['web/v1.0.0-dev.3'], version='web/v1.0.0-dev.2')
        self.assertTrue(result.startswith('claimed=false '), result)
        self.assertEqual(self.creates, [])
        self.assertEqual(
            self.handoff_descriptions('web/v1.0.0-dev.2'),
            [f"web/v1.0.0-dev.3 at {b} releases this commit's changes"],
        )

    def test_a_lane_claim_ignores_the_later_dev_tags_of_the_root_and_other_lanes(self):
        later = ['v1.0.0-dev.3', 'api/v1.0.0-dev.3', 'web/sub/v1.0.0-dev.3']
        result, _, _ = self.run_claim(tags_at_b=later, version='web/v1.0.0-dev.2')
        self.assertTrue(result.startswith('claimed=true '), result)
        self.assertEqual(len(self.creates), 1, self.creates)

    def test_a_version_held_at_another_commit_is_refused_whatever_later_tag_exists(self):
        _, a, b = self.run_claim(tags_at_b=['v1.0.0-dev.2', 'v1.0.0-dev.3'], claim_rc=1)
        self.assertIn(f'Tag v1.0.0-dev.2 exists at {b}, not {a}', self.log)
        self.assertEqual((self.creates, self.posts, self.output), ([], [], ''))

    def test_the_handoff_names_the_highest_of_two_dev_tags_on_one_commit(self):
        _, _, b = self.run_claim(
            tags_at_b=['v1.9.0-dev.4', 'v1.10.0-dev.1'], version='v1.9.0-dev.3'
        )
        self.assertEqual(
            self.handoff_descriptions('v1.9.0-dev.3'),
            [f"v1.10.0-dev.1 at {b} releases this commit's changes"],
        )

    def test_a_lower_version_on_a_commit_a_renumber_tagged_higher_hands_off(self):
        result, a, _ = self.run_claim(
            tags_at_a=['v1.2.0-dev.1', 'v1.10.0-dev.1'], version='v1.2.0-dev.1'
        )
        self.assertEqual(result, 'claimed=false handed_off_rc=0')
        self.assertEqual(self.creates, [])
        self.assertEqual(
            self.handoff_descriptions('v1.2.0-dev.1'),
            [f"v1.10.0-dev.1 at {a} releases this commit's changes"],
        )

    def test_the_higher_version_on_a_renumbered_commit_is_claimed_and_published(self):
        result, _, _ = self.run_claim(
            tags_at_a=['v1.2.0-dev.1', 'v1.10.0-dev.1'], version='v1.10.0-dev.1'
        )
        self.assertEqual(result, 'claimed=true handed_off_rc=1')
        self.assertEqual((self.creates, self.posts), ([], []))

    def test_a_root_claim_ignores_a_later_lane_dev_tag(self):
        result, _, _ = self.run_claim(tags_at_b=['web/v1.0.0-dev.3'])
        self.assertEqual(result, 'claimed=true handed_off_rc=1')
        self.assertEqual(len(self.creates), 1, self.creates)


class ClaimCheck(unittest.TestCase):
    """claim_held and claim_drop: a publishing job reads the claim job's tag, never creating one."""

    def run_claim(self, call, tags=None, read_fail=False):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='claim-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / 'gh').write_text(GH_STUB)
        (tmp / 'gh').chmod(0o755)
        state = tmp / 'state.json'
        state.write_text(
            json.dumps({'tags': tags or {}, 'creates': [], 'calls': [], 'read_fail': read_fail})
        )
        out = tmp / 'out'
        out.write_text('')
        script = (
            'set -euo pipefail\n'
            f'. "{SCRIPTS}/retry.sh"\n'
            f'. "{SCRIPTS}/tag-ref.sh"\n'
            'rc=0\n'
            f'{call} o/app v1.2.0 {COMMIT} || rc=$?\n'
            'echo "rc=${rc}"\n'
        )
        env = {
            **os.environ,
            'PATH': f'{tmp}:{os.environ["PATH"]}',
            'STUB_STATE': str(state),
            'READBACK_SLEEP': '0',
            'GITHUB_OUTPUT': str(out),
        }
        res = subprocess.run(
            ['bash', '-c', script], env=env, capture_output=True, text=True, check=False
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        doc = json.loads(state.read_text())
        writes = [
            c for c in doc['calls'] if c[:2] == ['api', 'repos/o/app/git/refs'] or 'DELETE' in c
        ]
        return res.stdout, out.read_text(), doc['tags'], writes

    def test_a_tag_at_this_commit_is_held(self):
        stdout, out, tags, writes = self.run_claim('claim_held', tags={'v1.2.0': COMMIT})
        self.assertEqual(
            (stdout, out, tags, writes), ('rc=0\n', 'claimed=true\n', {'v1.2.0': COMMIT}, [])
        )

    def test_a_tag_at_another_commit_fails_closed(self):
        stdout, out, _, writes = self.run_claim('claim_held', tags={'v1.2.0': 'b' * 40})
        self.assertIn(
            f"::error::tag v1.2.0 is at {'b' * 40}, not this run's commit {COMMIT}", stdout
        )
        self.assertEqual((stdout[-5:], out, writes), ('rc=1\n', '', []))

    def test_an_absent_tag_fails_closed_and_is_never_created(self):
        stdout, out, tags, writes = self.run_claim('claim_held')
        self.assertIn(f"::error::tag v1.2.0 is at <none>, not this run's commit {COMMIT}", stdout)
        self.assertEqual((stdout[-5:], out, tags, writes), ('rc=1\n', '', {}, []))

    def test_an_unreadable_tag_fails_closed_after_the_retries(self):
        stdout, out, _, writes = self.run_claim(
            'claim_held', tags={'v1.2.0': COMMIT}, read_fail=True
        )
        self.assertIn('::error::tag v1.2.0 cannot be read', stdout)
        self.assertEqual((stdout[-5:], out, writes), ('rc=1\n', '', []))

    def test_a_dropped_claim_deletes_this_commits_tag(self):
        stdout, _, tags, writes = self.run_claim('claim_drop', tags={'v1.2.0': COMMIT})
        self.assertEqual((stdout, tags), ('rc=0\n', {}))
        self.assertEqual(writes, [['api', '-X', 'DELETE', 'repos/o/app/git/refs/tags/v1.2.0']])

    def test_a_dropped_claim_leaves_another_commits_tag(self):
        stdout, _, tags, writes = self.run_claim('claim_drop', tags={'v1.2.0': 'b' * 40})
        self.assertIn("not this run's claim to delete", stdout)
        self.assertEqual((stdout[-5:], tags, writes), ('rc=1\n', {'v1.2.0': 'b' * 40}, []))

    def test_a_dropped_claim_already_gone_is_done(self):
        self.assertEqual(self.run_claim('claim_drop')[0::3], ('rc=0\n', []))

    def test_a_dropped_claim_github_cannot_read_is_kept(self):
        stdout, _, tags, writes = self.run_claim(
            'claim_drop', tags={'v1.2.0': COMMIT}, read_fail=True
        )
        self.assertIn('::error::tag v1.2.0 cannot be read', stdout)
        self.assertEqual((stdout[-5:], tags, writes), ('rc=1\n', {'v1.2.0': COMMIT}, []))


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
