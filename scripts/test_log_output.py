"""No target-written text reaches a scheduled job's log as a workflow command.

The jobs that read a target's files and run none of its code (the mutation calls'
discover, shard, merge, record, badge and aggregate jobs, and the weekly summary)
log through scripts/mutation_cost.py and scripts/gha-log.sh. Each script is fed
names and file contents holding the runner's command syntax, and every line it
writes is parsed with the runner's own grammar (actions/runner
src/Runner.Common/ActionCommand.cs TryParseV2 then TryParse). A command the
script did not write itself, at the start of the line, fails the test.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(os.environ.get('LOG_OUTPUT_SCRIPTS') or pathlib.Path(__file__).parent)
SHA = '0123456789abcdef0123456789abcdef01234567'
# actions/runner src/Runner.Worker/ActionCommandManager.cs: every registered extension and stop-commands.
REGISTERED = frozenset(
    {
        'internal-set-repo-path',
        'set-env',
        'set-output',
        'save-state',
        'add-mask',
        'add-path',
        'add-matcher',
        'remove-matcher',
        'debug',
        'warning',
        'error',
        'notice',
        'group',
        'endgroup',
        'echo',
        'stop-commands',
    }
)
OWN = ('notice', 'warning', 'error')
HOSTILE = (
    '::stop-commands::tok1',
    '\n::stop-commands::tok2',
    'x\r::add-mask::m',
    '%0A::set-env name=A::1',
    'y\n##[set-output name=o]1',
    '::warning::forged',
    'z\r\n::error file=a::forged',
    '%25%0D%0A::debug::d',
)
# .NET's StreamReader.ReadLine, which the runner reads a step's output with, ends a line at each of these.
LINE_END = re.compile(r'\r\n|\r|\n')


def parse(line: str, registered: set[str]) -> str | None:
    """The command the runner reads LINE as, or None."""
    v2 = line.lstrip()
    if v2.startswith('::'):
        end = v2.find('::', 2)
        if end >= 0 and (name := v2[2:end].split(' ')[0]) in registered:
            return name
    start = line.find('##[')
    if start >= 0:
        end = line.find(']', start)
        if end >= 0 and (name := line[start + 3 : end].split(' ', maxsplit=1)[0]) in registered:
            return name
    return None


def forged(text: str) -> list[str]:
    """Each line of TEXT the runner reads as a command the script did not write at its start."""
    registered = set(REGISTERED)
    bad = []
    for line in LINE_END.split(text):
        cmd = parse(line, registered)
        if cmd is None:
            continue
        if cmd in OWN and line.startswith(f'::{cmd}::'):
            continue
        bad.append(line)
        if cmd == 'stop-commands':
            registered.add(line.split('::', 2)[-1])
    return bad


class Parser(unittest.TestCase):
    def test_the_grammar_reads_the_runner_s_commands_and_only_those(self):
        cases = (
            ('::warning::x', 'warning'),
            ('  ::add-mask::x', 'add-mask'),
            ('- a ##[set-output name=x]1', 'set-output'),
            ('::warning x', None),
            ('::nope::x', None),
            ('- x%0A::stop-commands::t', None),
            ('##[nope]x', None),
        )
        for line, want in cases:
            with self.subTest(line=line):
                self.assertEqual(parse(line, set(REGISTERED)), want)
        self.assertEqual(forged('::warning::a\n::error::b'), [])
        self.assertEqual(forged('- a\n::stop-commands::t\n::t::'), ['::stop-commands::t', '::t::'])
        self.assertEqual(forged('::warning::a\r::add-mask::m'), ['::add-mask::m'])


class Scripts(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.n = 0
        self.logged: list[str] = []

    def dir(self) -> pathlib.Path:
        self.n += 1
        d = self.tmp / str(self.n)
        d.mkdir()
        return d

    def run_script(self, h: str, script: str, *args: str, log_stdout: bool = True) -> None:
        cmd = ['bash'] if script.endswith('.sh') else [sys.executable]
        proc = subprocess.run(
            [*cmd, str(SCRIPTS / script), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env={**os.environ, 'GITHUB_STEP_SUMMARY': str(self.tmp / 'summary.md')},
        )
        logged = proc.stderr + (proc.stdout if log_stdout else '')
        self.logged.append(logged)
        self.assertEqual(forged(logged), [], f'{script} {args} fed {h!r}:\n{logged[-1500:]}')

    def each(self, case) -> None:
        for h in HOSTILE:
            with self.subTest(hostile=h):
                self.logged = []
                case(h)
                # The fed text reached the log, escaped, so the case tested a line it wrote.
                word = re.search(r'stop-commands|add-mask|set-env|set-output|forged|debug', h)
                self.assertIn(word.group(0), ''.join(self.logged))

    @staticmethod
    def write(path: pathlib.Path, doc: object) -> pathlib.Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        return path

    def test_the_key_command(self):
        def case(h: str) -> None:
            self.run_script(h, 'mutation_cost.py', 'key', '--repo', h)
            self.run_script(h, 'mutation_cost.py', 'key', '--repo', 'a', '--dir', h)

        self.each(case)

    def test_stryker_discover(self):
        def case(h: str) -> None:
            root = self.dir()
            name = 'd' + h.replace('/', '_')
            self.write(root / name / 'stryker.config.json', '{}')
            self.write(root / 'w' / 'stryker.config.json', '{}')
            self.write(root / 'w' / 'stryker.weekly.json', json.dumps({'weekly': h}))
            self.run_script(
                h,
                'stryker-shard.py',
                'discover',
                '--repo',
                'a',
                '--sha',
                SHA,
                '--root',
                str(root),
                '--honour-opt-out',
                log_stdout=False,
            )
            self.run_script(
                h, 'stryker-shard.py', 'discover', '--repo', 'a', '--sha', SHA,
                '--root', str(root), '--dir', name, log_stdout=False,
            )  # fmt: skip

        self.each(case)

    def stryker_sizing(self, **over) -> dict:
        files = {f'src/f{i}.ts': {'run': 100, 'early': 0, 'prior_secs': 36000.0} for i in range(4)}
        return {
            'repo': 'r',
            'dir': 'web',
            'sha': SHA,
            'files': files,
            'shardable': True,
            'break': None,
            'workers': 4,
            'runners': 4,
            'setup_secs': 90.0,
            **over,
        }

    def test_stryker_matrix(self):
        entry = {'repo': 'r', 'sha': SHA, 'dir': 'web', 'key': 'r~web'}

        def case(h: str) -> None:
            for plan in (
                self.stryker_sizing(repo=h),
                self.stryker_sizing(
                    shardable=False,
                    files={f'src/{h}.ts': {'run': 100, 'early': 0, 'prior_secs': 3.6e6}},
                ),
                h,
            ):
                d = self.dir()
                self.write(d / 'plans' / 'r~web.json', plan)
                self.write(
                    d / 'history' / 'r~web.json',
                    {'files': {h: {'secs': 1, 'mutants': 1, h: 1}}, 'setup_secs': h},
                )
                self.run_script(
                    h, 'stryker-shard.py', 'matrix', '--entries', json.dumps([entry]),
                    '--plans', str(d / 'plans'),
                    '--history', str(d / 'history'), '--out-plans', str(d / 'out'),
                    log_stdout=False,
                )  # fmt: skip

        self.each(case)

    def test_stryker_merge(self):
        def case(h: str) -> None:
            d = self.dir()
            plan = {
                'repo': 'r',
                'dir': 'web',
                'sha': SHA,
                'break': None,
                'workers': 4,
                'shards': 2,
                'capped': h,
                'projected_minutes': [300.0, None],
                'files': {
                    'src/a.ts': {'shard': 1, 'run': 1, 'early': 0},
                    f'src/{h}.ts': {'shard': 1, 'run': 1, 'early': 0},
                    'src/b.ts': {'shard': 2, 'run': 1, 'early': 0},
                },
                'mutants': None,
                'history': None,
            }
            self.write(d / 'plan.json', plan)
            self.write(
                d / 'shards' / '1' / 'meta.json',
                {'repo': 'r', 'dir': 'web', 'shard': 1, 'complete': True},
            )
            self.write(d / 'shards' / '1' / 'result.json', {'files': {'src/a.ts': {'mutants': []}}})
            self.write(d / 'shards' / '1' / 'costs.json', {'files': {h: {'secs': 1, 'mutants': 1}}})
            self.run_script(
                h, 'stryker-shard.py', 'merge', '--plan', str(d / 'plan.json'),
                '--shards', str(d / 'shards'), '--stage', str(d / 'stage'), '--costs', str(d / 'c'),
            )  # fmt: skip

        self.each(case)

    def gremlins_sizing(self, **over) -> dict:
        module = {
            'mode': 'per-package',
            'workers': 4,
            'setup_secs': 60.0,
            'packages': {},
            'files': {f'p{i}/x.go': {'runnable': 100, 'prior_secs': 36000.0} for i in range(4)},
        }
        return {'repo': 'r', 'sha': SHA, 'modules': {'.': module}, **over}

    def test_gremlins_matrix(self):
        def case(h: str) -> None:
            base = self.gremlins_sizing()
            for plan in (
                self.gremlins_sizing(repo=h),
                {**base, 'modules': {h: base['modules']['.']}},
                {
                    **base,
                    'modules': {'.': {**base['modules']['.'], 'files': {h: {'runnable': 1}}}},
                },
                h,
            ):
                d = self.dir()
                self.write(d / 'sizing.json', plan)
                self.write(d / 'history.json', {'repo': 'r', 'modules': {h: {'files': {}, h: 1}}})
                self.run_script(
                    h, 'gremlins-shard.py', 'matrix', '--repo', 'r', '--plan', str(d / 'sizing.json'),
                    '--attempts', '1', '--history', str(d / 'history.json'),
                    '--out-plan', str(d / 'plan.json'), log_stdout=False,
                )  # fmt: skip
            self.run_script(
                h, 'gremlins-shard.py', 'matrix', '--repo', h, '--plan', str(self.tmp / 'none'),
                '--attempts', '1', log_stdout=False,
            )  # fmt: skip

        self.each(case)

    def test_gremlins_capped_history_plan_and_costs(self):
        def case(h: str) -> None:
            d = self.dir()
            plan = self.write(
                d / 'plan.json',
                {'capped': h, 'shards': 2, 'projected_minutes': [300.0], 'unsized_shared': [h]},
            )
            self.run_script(h, 'gremlins-shard.py', 'capped', '--repo', 'r', '--plan', str(plan))
            prev = self.write(d / 'prev.json', {'repo': 'r', 'modules': {h: {'files': {h: 1}}}})
            self.write(d / 'art' / h.replace('/', '_') / 'meta.json', {'repo': 'r'})
            self.write(
                d / 'art' / h.replace('/', '_') / 'gremlins-costs.json',
                {'modules': {h: {'files': {h: {'secs': 1, 'mutants': 1}}, 'setup_secs': 1}}},
            )
            self.run_script(
                h, 'gremlins-shard.py', 'history', '--repo', 'r', '--artifacts-dir', str(d / 'art'),
                '--previous', str(prev), '--plan', str(plan), '--out', str(d / 'out.json'),
            )  # fmt: skip
            sized = d / 'sizing' / h.replace('/', '_')
            self.write(sized / 'plan.env', f'sized=1\nmode={h}\nworkers=1\nsetup_secs=1\n')
            self.run_script(
                h, 'gremlins-shard.py', 'plan', '--repo', 'r', '--sha', SHA,
                '--module', f'{h}={sized}', '--out', str(d / 'sized.json'),
            )  # fmt: skip
            self.run_script(
                h, 'gremlins-shard.py', 'costs', '--module', f'{h}={sized / "missing"}',
                '--out', str(d / 'costs.json'),
            )  # fmt: skip

        self.each(case)

    def test_the_call_record_and_the_fleet_summary(self):
        def case(h: str) -> None:
            d = self.dir()
            ok = {'result': 'success', 'outputs': {}}
            needs = {
                'discover': {
                    'result': 'success',
                    'outputs': {
                        'entries': json.dumps([{'dir': 'w', 'key': 'a~w'}]),
                        'refused': json.dumps([{'dir': h, 'reason': h}]),
                        'opted_out': json.dumps([h]),
                    },
                },
                'plan': ok,
                'shard': {'result': 'success', 'outputs': {'merge': json.dumps([{'dir': 'w'}])}},
                'run': ok,
                'merge': ok,
                h: {'result': 'failure'},
            }
            self.run_script(
                h, 'mutation-summary.py', 'record', '--tool', 'stryker', '--repo', 'a',
                '--target', h, '--key', 'a', '--needs', json.dumps(needs), '--out', str(d / 'a.json'),
            )  # fmt: skip
            self.write(d / 'b.json', h)
            self.write(d / 'c.json', {'tool': h, 'key': 'c', 'jobs': {}})
            self.run_script(
                h, 'mutation-summary.py', 'fleet', '--tool', 'stryker',
                '--keys', json.dumps(['a', 'b', 'c', h]), '--results', str(d),
            )  # fmt: skip

        self.each(case)

    def test_the_refused_command(self):
        def case(h: str) -> None:
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / 'mutation_cost.py'), 'refused', '--repo', 'a'],
                input=f'ok\0{h}\0'.encode(),
                capture_output=True,
                check=False,
                timeout=60,
            )
            logged = proc.stderr.decode()
            self.logged.append(logged)
            self.assertEqual(forged(logged), [], logged)
            self.assertEqual(json.loads(proc.stdout)[0]['dir'], h)

        self.each(case)

    def test_the_aggregates_and_the_artifact_discovery(self):
        def case(h: str) -> None:
            d = self.dir()
            name = h.replace('/', '_')
            self.write(d / 's' / name / 'meta.json', {'repo': 'r', 'dir': h})
            self.write(d / 's' / name / 'mutation.json', h)
            self.run_script(
                h, 'stryker-aggregate.py', '--repo', 'r', '--artifacts-dir', str(d / 's'),
                '--week', 'w', '--run-url', 'u', log_stdout=False,
            )  # fmt: skip
            meta = {'repo': 'r', 'attempt': '1', 'shard': h, 'shards': h}
            self.write(d / 'g' / 'a' / 'meta.json', meta)
            self.write(d / 'g' / 'a' / 'gremlins-out.json', {'files': []})
            self.write(d / 'g' / name / 'gremlins-out.json', {'files': []})
            self.write(d / 'g' / 'b' / 'meta.json', {'repo': 'r', 'attempt': '2'})
            self.write(d / 'g' / 'b' / 'gremlins-out.json', {'files': [], 'mode': h})
            self.run_script(
                h, 'gremlins-aggregate.py', '--repo', 'r', '--artifacts-dir', str(d / 'g'),
                '--config', str(SCRIPTS.parent / 'configs' / 'gremlins.yaml'),
                '--week', 'w', '--run-url', 'u', log_stdout=False,
            )  # fmt: skip
            self.write(d / 'm' / name / 'meta.json', {'repo': h})
            self.write(d / 'm' / 'ok' / 'meta.json', {'repo': 'b'})
            self.run_script(h, 'discover-artifact-repos.sh', '--allow', 'a', str(d / 'm'))
            self.run_script(h, 'discover-artifact-repos.sh', '--allow', 'a', str(d / 'm' / name))

        self.each(case)


if __name__ == '__main__':
    unittest.main()
