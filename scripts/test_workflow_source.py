"""Reusable workflows run this repository's code from their own commit.

A job that needs a script or composite action from here checks out
`job.workflow_repository` at `job.workflow_sha` beside the consumer's tree and
runs it from there, so a caller's pin fixes every line of ci code its run
executes. A tag reference to this repository's actions would not.
"""

from __future__ import annotations

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / '.github' / 'workflows').glob('*.y*ml'))
ACTIONS = sorted((ROOT / 'actions').glob('*/action.yml'))
SOURCE = '.cplieger-ci'
SOURCE_STEP = 'Check out the ci source'
OWN_REF = re.compile(r'^cplieger/ci/[^@]+@(?P<ref>.+)$')
SHA = re.compile(r'^[0-9a-f]{40}$')


def jobs_of(path: pathlib.Path):
    for name, job in (yaml.safe_load(path.read_text())['jobs'] or {}).items():
        yield f'{path.name}:{name}', job


def jobs():
    for path in WORKFLOWS:
        yield from jobs_of(path)


def uses_refs():
    for path in WORKFLOWS:
        for label, job in jobs_of(path):
            refs = [job.get('uses', ''), *(s.get('uses', '') for s in job.get('steps') or [])]
            yield from ((label, r) for r in refs if r)
    for path in ACTIONS:
        steps = yaml.safe_load(path.read_text())['runs'].get('steps') or []
        yield from ((path.parent.name, s['uses']) for s in steps if 'uses' in s)


def uses_source(step: dict) -> bool:
    run = step.get('run', '')
    return (
        SOURCE in step.get('uses', '')
        or SOURCE in str(step.get('env', ''))
        or SOURCE in run
        # An expansion, not the `[$]CI_TOOLS/` pattern self-release greps workflows for.
        or re.search(r'\$\{?CI_TOOLS\b', run) is not None
    )


class OwnReferences(unittest.TestCase):
    def test_no_step_reaches_this_repository_through_a_moving_ref(self):
        own = 0
        for label, ref in uses_refs():
            m = OWN_REF.match(ref)
            if m:
                own += 1
                with self.subTest(where=label, uses=ref):
                    self.assertRegex(m['ref'], SHA)
        self.assertGreater(own, 0)


class SourceCheckout(unittest.TestCase):
    def test_every_job_that_runs_the_source_checks_it_out_first(self):
        users = set()
        for label, job in jobs():
            steps = job.get('steps') or []
            first_use = next((i for i, s in enumerate(steps) if uses_source(s)), None)
            if first_use is None:
                continue
            users.add(label)
            with self.subTest(job=label):
                names = [s.get('name') for s in steps]
                self.assertIn(SOURCE_STEP, names)
                at = names.index(SOURCE_STEP)
                self.assertLess(at, first_use)
                self.assertEqual(
                    steps[at]['with'],
                    {
                        'repository': '${{ job.workflow_repository }}',
                        'ref': '${{ job.workflow_sha }}',
                        'path': SOURCE,
                        'persist-credentials': False,
                    },
                )
                self.assertTrue(steps[at]['uses'].startswith('actions/checkout@'))
                # A later root checkout would empty the workspace and the source with it.
                later = [
                    s.get('name', '')
                    for s in steps[at + 1 :]
                    if s.get('uses', '').startswith('actions/checkout@')
                    and 'path' not in (s.get('with') or {})
                ]
                self.assertEqual(later, [])
        expected = {'release.yaml:detect', 'release.yaml:go-nested', 'docker-release.yaml:finalize'}
        self.assertLessEqual({*expected, 'ci.yaml:pr-policy'}, users)

    def test_a_run_body_naming_the_source_counts_as_a_use(self):
        self.assertTrue(uses_source({'run': f'python3 {SOURCE}/scripts/intake.py'}))
        # A job-level CI_TOOLS reaches the step only through its run body.
        self.assertTrue(uses_source({'run': '"$CI_TOOLS/release-state.sh" pending'}))
        self.assertTrue(uses_source({'run': 'bash "${CI_TOOLS}/retry.sh" 3'}))
        self.assertFalse(uses_source({'run': "grep -ohE '[$]CI_TOOLS/[A-Za-z0-9_.-]+' a.yaml"}))
        self.assertFalse(uses_source({'run': 'git fetch origin', 'with': {'path': SOURCE}}))

    def test_the_root_npm_publish_runs_with_the_source_moved_out_of_the_package(self):
        job = yaml.safe_load((ROOT / '.github/workflows/release.yaml').read_text())['jobs'][
            'renumber-npm'
        ]
        names = [s.get('name') for s in job['steps']]
        move = job['steps'][names.index('Move the ci source out of the package')]
        self.assertIn(f'mv {SOURCE} "$RUNNER_TEMP/cplieger-ci"', move['run'])
        self.assertEqual(names.index(SOURCE_STEP) + 1, names.index(move['name']))
        tools = {s['env']['CI_TOOLS'] for s in job['steps'] if 'CI_TOOLS' in s.get('env', {})}
        self.assertEqual(tools, {'${{ runner.temp }}/cplieger-ci/scripts'})


class ActionlintCarveOut(unittest.TestCase):
    """Each ignore stays scoped to the files and messages that need it."""

    def setUp(self):
        config = yaml.safe_load((ROOT / '.github/actionlint.yaml').read_text())
        ((self.glob, rule), (self.queue_glob, queue_rule)) = config['paths'].items()
        (self.pattern,) = rule['ignore']
        (self.queue_pattern,) = queue_rule['ignore']

    def test_the_ignore_names_exactly_the_workflows_that_read_the_job_identity(self):
        listed = set(re.fullmatch(r'\.github/workflows/\{(.+)\}\.yaml', self.glob)[1].split(','))
        readers = {p.stem for p in WORKFLOWS if 'job.workflow_' in p.read_text()}
        self.assertEqual(listed, readers)

    def test_the_ignore_matches_only_the_two_properties_of_the_job_context(self):
        job = 'is not defined in object type {check_run_id: number; container: {id: string}}'
        self.assertRegex(f'property "workflow_sha" {job}', self.pattern)
        self.assertRegex(f'property "workflow_repository" {job}', self.pattern)
        self.assertNotRegex(f'property "workflow_ref" {job}', self.pattern)
        self.assertNotRegex(
            'property "workflow_sha" is not defined in object type {ref: string}', self.pattern
        )

    def test_the_queue_ignore_names_exactly_the_workflows_that_queue(self):
        def queues(doc):
            groups = [doc.get('concurrency'), *(j.get('concurrency') for j in doc['jobs'].values())]
            return any(isinstance(g, dict) and 'queue' in g for g in groups)

        queued = {
            f'.github/workflows/{p.name}'
            for p in WORKFLOWS
            if queues(yaml.safe_load(p.read_text()))
        }
        self.assertEqual({self.queue_glob}, queued)
        message = 'unexpected key "queue" for "concurrency" section'
        self.assertRegex(message, self.queue_pattern)
        self.assertNotRegex(message.replace('queue', 'group'), self.queue_pattern)


if __name__ == '__main__':
    unittest.main()
