"""Tests for workflow_replay.py: GitHub expression semantics and step replay."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import ClassVar

import workflow_replay as wr
import yaml


def ev(text: str, **contexts):
    return wr.Scope({'steps': {}, **contexts}, {}, []).evaluate(text)


class Expressions(unittest.TestCase):
    def test_and_or_return_an_operand_not_a_boolean(self):
        self.assertEqual(ev("'dev' == 'dev' && 'v1.1.0' || ''"), 'v1.1.0')
        self.assertEqual(ev("'dev' == 'main' && 'v1.1.0' || ''"), '')
        self.assertEqual(ev("a.x || 'false'", a={'x': ''}), 'false')

    def test_string_equality_ignores_case(self):
        self.assertTrue(ev("'Two-Branch' == 'two-branch'"))

    def test_mixed_types_compare_as_numbers(self):
        self.assertTrue(ev("a.n == '1'", a={'n': 1.0}))
        self.assertTrue(ev("a.missing == ''", a={}))

    def test_fromjson_index_and_property(self):
        state = json.dumps(
            {'.': {'in_range': True, 'kind_note': 'k'}, 'yamlenv': {'in_range': False}}
        )
        outs = {'detect': {'outputs': {'lane_state': state}}}
        self.assertEqual(
            ev(
                "fromJSON(needs.detect.outputs.lane_state || '{}')[matrix.dir].in_range && 'true' || 'false'",
                needs=outs,
                matrix={'dir': 'yamlenv'},
            ),
            'false',
        )
        self.assertEqual(
            ev("fromJSON(needs.detect.outputs.lane_state)['.'].kind_note", needs=outs), 'k'
        )

    def test_not_and_status_functions(self):
        self.assertTrue(
            ev("!cancelled() && needs.s.result == 'success'", needs={'s': {'result': 'success'}})
        )
        self.assertFalse(ev('!inputs.dry_run', inputs={'dry_run': True}))

    def test_an_unknown_context_or_function_is_refused(self):
        with self.assertRaises(wr.ReplayError):
            ev('vars.X')
        with self.assertRaises(wr.ReplayError):
            ev('hashFiles(a)', a={})

    def test_render_whole_and_embedded(self):
        scope = wr.Scope({'a': {'v': True, 'w': 'x'}}, {}, [])
        self.assertEqual(scope.render('${{ a.v }}'), 'true')
        self.assertEqual(scope.render('p-${{ a.w }}-q'), 'p-x-q')


class JobConditions(unittest.TestCase):
    WF: ClassVar[dict] = {
        'jobs': {
            'a': {},
            'b': {'needs': 'a'},
            'c': {'needs': ['b'], 'if': "${{ needs.b.outputs.x == 'y' }}"},
            'd': {'needs': ['b'], 'if': "${{ !cancelled() && needs.b.result == 'success' }}"},
            'e': {'needs': 'b'},
        }
    }

    def runs(self, job, a='success', b='success'):
        needs = {'a': {'result': a}, 'b': {'result': b, 'outputs': {'x': 'y'}}}
        return wr.job_condition(self.WF, job, {'needs': needs}, [])

    def test_a_condition_with_no_status_function_needs_every_ancestor_green(self):
        self.assertTrue(self.runs('c'))
        self.assertFalse(self.runs('c', a='failure'), 'a failed grandparent')
        self.assertFalse(self.runs('c', a='skipped'))
        self.assertFalse(self.runs('e', a='failure'), 'no if: is success()')

    def test_an_explicit_status_function_replaces_the_implicit_success(self):
        self.assertTrue(self.runs('d', a='failure'))
        self.assertFalse(self.runs('d', b='failure'))

    def test_failure_reads_the_ancestors(self):
        wf = {'jobs': {**self.WF['jobs'], 'f': {'needs': 'b', 'if': 'failure()'}}}
        needs = {'a': {'result': 'failure'}, 'b': {'result': 'skipped'}}
        self.assertTrue(wr.job_condition(wf, 'f', {'needs': needs}, []))

    def test_a_status_function_with_an_ancestor_result_missing_is_refused(self):
        with self.assertRaisesRegex(wr.ReplayError, r"ancestor job\(s\) \['a'\]"):
            wr.job_condition(self.WF, 'c', {'needs': {'b': {'result': 'success'}}}, [])


class Steps(unittest.TestCase):
    def replay(self, workflow: dict, context: dict, *extra: str) -> tuple[int, dict]:
        with (
            tempfile.TemporaryDirectory() as tmp,
            unittest.mock.patch.object(tempfile, 'tempdir', tmp),
        ):
            wf, ctx, out = Path(tmp, 'w.yaml'), Path(tmp, 'c.json'), Path(tmp, 'o.json')
            wf.write_text(yaml.safe_dump(workflow))
            ctx.write_text(json.dumps(context))
            rc = wr.main(
                [
                    '--workflow',
                    str(wf),
                    '--job',
                    'j',
                    '--context',
                    str(ctx),
                    '--cwd',
                    tmp,
                    '--out',
                    str(out),
                    *extra,
                ]
            )
            return rc, json.loads(out.read_text())

    def test_a_step_output_reaches_a_later_step_and_the_job_outputs(self):
        wf = {
            'jobs': {
                'j': {
                    'outputs': {'v': '${{ steps.b.outputs.v }}'},
                    'steps': [
                        {
                            'id': 'a',
                            'run': 'echo "v=$IN" >> "$GITHUB_OUTPUT"',
                            'env': {'IN': '${{ inputs.x }}'},
                        },
                        {
                            'id': 'b',
                            'if': "steps.a.outputs.v == 'one'",
                            'run': 'echo "v=${A}2" >> "$GITHUB_OUTPUT"',
                            'env': {'A': '${{ steps.a.outputs.v }}'},
                        },
                    ],
                }
            }
        }
        rc, res = self.replay(wf, {'inputs': {'x': 'one'}})
        self.assertEqual((rc, res['outputs']), (0, {'v': 'one2'}))

    def test_a_false_job_condition_runs_nothing(self):
        wf = {'jobs': {'j': {'if': "inputs.x == 'y'", 'steps': [{'run': 'exit 1'}]}}}
        rc, res = self.replay(wf, {'inputs': {'x': 'n'}})
        self.assertEqual((rc, res['skipped'], res['ran']), (0, True, []))

    def test_an_external_action_must_be_skipped_explicitly(self):
        wf = {'jobs': {'j': {'steps': [{'name': 'Install', 'uses': 'some/action@v1'}]}}}
        self.assertEqual(self.replay(wf, {})[0], 1)
        self.assertEqual(self.replay(wf, {}, '--skip', 'Install')[0], 0)

    def test_a_failing_step_fails_the_replay_naming_it(self):
        wf = {
            'jobs': {
                'j': {
                    'steps': [{'name': 'Boom', 'run': 'exit 3'}, {'name': 'After', 'run': 'true'}]
                }
            }
        }
        rc, res = self.replay(wf, {})
        self.assertEqual(rc, 1)
        self.assertIn("step 'Boom' exited 3", res['failed'])
        self.assertNotIn('After', res['ran'])

    def test_a_failing_hook_fails_the_replay_before_its_step(self):
        wf = {'jobs': {'j': {'steps': [{'name': 'Step', 'run': 'true'}]}}}
        rc, res = self.replay(wf, {}, '--hook', 'Step=exit 4')
        self.assertEqual(rc, 1)
        self.assertIn("the hook before 'Step' exited 4", res['failed'])
        self.assertEqual(res['ran'], [])


SOURCE = {
    'name': 'Check out the ci source',
    'uses': 'actions/checkout@0000000000000000000000000000000000000000',
    'with': {
        'repository': '${{ job.workflow_repository }}',
        'ref': '${{ job.workflow_sha }}',
        'path': '.src',
    },
}


class CiSource(unittest.TestCase):
    """A job that checks out its own workflow's source and runs actions from it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / 'ci'
        action = self.root / 'actions' / 'say'
        action.mkdir(parents=True)
        (action / 'action.yml').write_text(
            yaml.safe_dump(
                {
                    'inputs': {'word': {'default': ''}},
                    'outputs': {'said': {'value': '${{ steps.s.outputs.said }}'}},
                    'runs': {
                        'using': 'composite',
                        'steps': [
                            {
                                'id': 's',
                                'shell': 'bash',
                                'env': {'W': '${{ inputs.word }}'},
                                'run': 'echo "said=$W:$(cat "$GITHUB_ACTION_PATH/../../marker")"'
                                ' >> "$GITHUB_OUTPUT"',
                            }
                        ],
                    },
                }
            )
        )
        (self.root / 'marker').write_text('root\n')
        self.cwd = self.tmp / 'consumer'
        self.cwd.mkdir()

    def replay(self, steps: list, context: dict | None = None) -> tuple[int, dict]:
        wf = {'jobs': {'j': {'outputs': {'said': '${{ steps.a.outputs.said }}'}, 'steps': steps}}}
        paths = {k: self.tmp / k for k in ('w.yaml', 'c.json', 'o.json')}
        paths['w.yaml'].write_text(yaml.safe_dump(wf))
        paths['c.json'].write_text(json.dumps(context or {}))
        with unittest.mock.patch.object(tempfile, 'tempdir', str(self.tmp)):
            rc = wr.main(
                [
                    *('--workflow', str(paths['w.yaml']), '--job', 'j'),
                    *('--context', str(paths['c.json']), '--cwd', str(self.cwd)),
                    *('--out', str(paths['o.json']), '--actions-root', str(self.root)),
                ]
            )
        return rc, json.loads(paths['o.json'].read_text())

    def test_a_local_action_runs_from_the_checked_out_source(self):
        rc, res = self.replay(
            [SOURCE, {'id': 'a', 'uses': './.src/actions/say', 'with': {'word': 'hi'}}]
        )
        self.assertEqual((rc, res['outputs']), (0, {'said': 'hi:root'}))
        self.assertEqual((self.cwd / '.src').resolve(), self.root.resolve())
        self.assertIn('Check out the ci source (checkout: --actions-root)', res['ran'])

    def test_a_local_action_without_the_checkout_is_refused(self):
        rc, res = self.replay([{'id': 'a', 'uses': './.src/actions/say'}])
        self.assertEqual(rc, 1)
        self.assertIn('no action at', res['failed'])

    def test_a_path_checkout_of_another_repository_is_refused(self):
        other = {**SOURCE, 'with': {**SOURCE['with'], 'repository': 'someone/else'}}
        rc, res = self.replay([other])
        self.assertEqual(rc, 1)
        self.assertIn("checkout of 'someone/else' into a path is not replayed", res['failed'])

    def test_the_job_context_names_the_ci_repository_unless_the_context_overrides_it(self):
        echo = {
            'id': 'a',
            'run': 'echo "said=$R@$S" >> "$GITHUB_OUTPUT"',
            'env': {'R': '${{ job.workflow_repository }}', 'S': '${{ job.workflow_sha }}'},
        }
        self.assertEqual(self.replay([echo])[1]['outputs'], {'said': 'cplieger/ci@' + '0' * 40})
        ctx = {'job': {'workflow_sha': 'abc'}}
        self.assertEqual(self.replay([echo], ctx)[1]['outputs'], {'said': 'cplieger/ci@abc'})


if __name__ == '__main__':
    unittest.main()
