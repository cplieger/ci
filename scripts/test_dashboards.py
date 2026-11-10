"""scripts/dashboards.sh, the ci.yaml step that lists a repository's dashboards, and the
dashboard-check action's loop over them."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from dashboard_check_fixtures import ACTION, ROOT, TWO_DASHBOARDS

SCRIPT = ROOT / 'scripts' / 'dashboards.sh'
REFUSED = (
    'grafana-dashboard-.json',
    'grafana-dashboard-Messages.json',
    'grafana-dashboard-a_b.json',
    'grafana-dashboard-a.b.json',
    'grafana-dashboard-a--b.json',
    'grafana-dashboard--a.json',
    'grafana-dashboard-a-.json',
    'grafana-dashboard-a b.json',
)
GIT_ENV = {'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null'}


def run(
    args: list[str], cwd: pathlib.Path, stdin: str = '', env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        input=stdin,
        env={**os.environ, **GIT_ENV, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


class Script(unittest.TestCase):
    def tmp(self) -> pathlib.Path:
        return pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))

    def dashboards(
        self, *args: str, cwd: pathlib.Path, stdin: str = ''
    ) -> subprocess.CompletedProcess:
        return run(['bash', str(SCRIPT), *args], cwd, stdin)

    def test_list_prints_the_main_file_first_then_the_others_in_byte_order(self):
        tmp = self.tmp()
        for name in (
            'grafana-dashboard-messages.json',
            'grafana-dashboard-a1.json',
            'grafana-dashboard.json',
        ):
            (tmp / name).write_text('{}')
        (tmp / 'grafana-dashboard-dir.json').mkdir()
        (tmp / 'grafana-dashboards').mkdir()
        (tmp / 'grafana-dashboardx.json').write_text('{}')
        (tmp / 'sub').mkdir()
        (tmp / 'sub' / 'grafana-dashboard-nested.json').write_text('{}')
        result = self.dashboards('list', cwd=tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split(),
            [
                'grafana-dashboard.json',
                'grafana-dashboard-a1.json',
                'grafana-dashboard-messages.json',
            ],
        )

    def test_list_prints_nothing_without_a_dashboard_and_a_suffixed_file_alone(self):
        tmp = self.tmp()
        self.assertEqual(self.dashboards('list', cwd=tmp).stdout, '')
        (tmp / 'grafana-dashboard-messages.json').write_text('{}')
        self.assertEqual(
            self.dashboards('list', cwd=tmp).stdout, 'grafana-dashboard-messages.json\n'
        )

    def test_every_mode_refuses_a_suffix_outside_the_rule_by_name(self):
        for name in REFUSED:
            with self.subTest(name=name):
                tmp = self.tmp()
                (tmp / 'grafana-dashboard.json').write_text('{}')
                (tmp / name).write_text('{}')
                for args, stdin in (
                    (['list'], ''),
                    (['filter'], f'{name}\n'),
                    (['package', name], ''),
                ):
                    result = self.dashboards(*args, cwd=tmp, stdin=stdin)
                    self.assertEqual(result.returncode, 1, args)
                    self.assertEqual(result.stdout, '', args)
                    self.assertIn(f'::error::{name} cannot be published', result.stderr)

    def test_package_names_the_ghcr_package_under_the_image(self):
        tmp = self.tmp()
        for name, package in (
            ('grafana-dashboard.json', 'dashboard'),
            ('grafana-dashboard-messages.json', 'dashboard-messages'),
            ('grafana-dashboard-logs-2.json', 'dashboard-logs-2'),
            ('grafana-dashboard-7.json', 'dashboard-7'),
        ):
            with self.subTest(name=name):
                result = self.dashboards('package', name, cwd=tmp)
                self.assertEqual(
                    (result.returncode, result.stdout), (0, f'{package}\n'), result.stderr
                )

    def test_filter_keeps_each_dashboard_name_once(self):
        stdin = 'grafana-dashboard-b.json\nREADME.md\ngrafana-dashboard-a.json\ngrafana-dashboard-b.json\ngrafana-dashboard.json\n'
        result = self.dashboards('filter', cwd=self.tmp(), stdin=stdin)
        self.assertEqual(
            result.stdout.split(),
            ['grafana-dashboard.json', 'grafana-dashboard-a.json', 'grafana-dashboard-b.json'],
        )

    def tree(self, files: dict[str, str], dirs: tuple[str, ...] = ()) -> tuple[pathlib.Path, str]:
        repo = self.tmp()
        run(['git', 'init', '-q', '.'], repo)
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
        for name in dirs:
            (repo / name).mkdir()
            (repo / name / 'x').write_text('x')
        run(['git', 'add', '.'], repo)
        tree = run(['git', 'write-tree'], repo).stdout.strip()
        self.assertRegex(tree, '^[0-9a-f]{40}$')
        return repo, tree

    def test_list_tree_reads_the_tree_root_not_the_working_directory(self):
        repo, tree = self.tree(
            {
                'grafana-dashboard.json': '{}',
                'grafana-dashboard-messages.json': '{}',
                'docs/grafana-dashboard-nested.json': '{}',
            },
            dirs=('grafana-dashboard-dir.json',),
        )
        (repo / 'grafana-dashboard-untracked.json').write_text('{}')
        result = self.dashboards('list-tree', tree, cwd=repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split(), ['grafana-dashboard.json', 'grafana-dashboard-messages.json']
        )

    def test_list_tree_refuses_a_bad_suffix_and_an_unreadable_tree(self):
        repo, tree = self.tree({'grafana-dashboard-Bad.json': '{}'})
        result = self.dashboards('list-tree', tree, cwd=repo)
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertIn('::error::grafana-dashboard-Bad.json cannot be published', result.stderr)
        result = self.dashboards('list-tree', 'no-such-tree', cwd=repo)
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertIn(
            '::error::git cannot list the files at the root of no-such-tree', result.stderr
        )

    def test_a_name_git_quotes_or_a_newline_splits_is_refused_whole(self):
        for name in (
            'grafana-dashboard-é.json',
            'grafana-dashboard-a\\b.json',
            'grafana-dashboard-a\tb.json',
            'grafana-dashboard-a\rb.json',
            'grafana-dashboard-a\nb.json',
        ):
            repo, tree = self.tree({'grafana-dashboard.json': '{}', name: '{}'})
            encoded = name.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
            for args in (['list'], ['list-tree', tree]):
                with self.subTest(name=name, mode=args[0]):
                    result = self.dashboards(*args, cwd=repo)
                    self.assertEqual((result.returncode, result.stdout), (1, ''))
                    self.assertIn(f'::error::{encoded} cannot be published', result.stderr)
                    self.assertEqual(result.stderr.count('\n'), 1, result.stderr)

    def test_a_refused_name_cannot_open_another_workflow_command(self):
        repo, tree = self.tree(
            {'grafana-dashboard.json': '{}', 'grafana-dashboard-x\n::add-mask::y%.json': '{}'}
        )
        for args in (['list'], ['list-tree', tree]):
            with self.subTest(mode=args[0]):
                result = self.dashboards(*args, cwd=repo)
                self.assertEqual((result.returncode, result.stdout), (1, ''))
                lines = result.stderr.splitlines()
                self.assertEqual(len(lines), 1, result.stderr)
                self.assertTrue(lines[0].startswith('::error::'), lines[0])
                self.assertIn('grafana-dashboard-x%0A::add-mask::y%25.json', lines[0])

    def test_an_unknown_mode_is_a_usage_error(self):
        result = self.dashboards('nope', cwd=self.tmp())
        self.assertEqual(result.returncode, 1)
        self.assertIn('usage: dashboards.sh', result.stderr)


class CiListing(unittest.TestCase):
    """The repo job lists the dashboards and hands every one to the check."""

    def steps(self) -> dict[str, dict]:
        workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yaml').read_text())
        return {s.get('name'): s for s in workflow['jobs']['repo']['steps'] if s.get('name')}

    def test_the_check_runs_on_the_listing_and_only_when_a_dashboard_exists(self):
        steps = self.steps()
        either = "${{ hashFiles('grafana-dashboard.json', 'grafana-dashboard-*.json') != '' }}"
        self.assertEqual(steps['Check out the ci source']['if'], either)
        self.assertEqual(steps['List the dashboards']['if'], either)
        self.assertEqual(steps['List the dashboards']['id'], 'dashboards')
        check = steps['Dashboard check']
        self.assertEqual(
            check['if'],
            "${{ hashFiles('grafana-dashboard.json', 'grafana-dashboard-*.json') != ''"
            " && steps.dashboards.outputs.files != '' }}",
        )
        self.assertEqual(check['uses'], './.cplieger-ci/actions/dashboard-check')
        self.assertEqual(check['with'], {'path': '${{ steps.dashboards.outputs.files }}'})
        self.assertTrue(check['continue-on-error'])
        names = list(steps)
        self.assertLess(names.index('Check out the ci source'), names.index('List the dashboards'))
        self.assertLess(names.index('List the dashboards'), names.index('Dashboard check'))

    def listing(self, files: tuple[str, ...]) -> tuple[subprocess.CompletedProcess, str, str]:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        repo = tmp / 'repo'
        repo.mkdir()
        for name in files:
            source = TWO_DASHBOARDS / name
            (repo / name).write_text(source.read_text() if source.exists() else '{}')
        marker = tmp / 'failures'
        body = self.steps()['List the dashboards']['run'].replace('/tmp/_ci_failures', str(marker))
        result = run(
            ['bash', '-e', '-c', body],
            repo,
            env={'CI_TOOLS': str(ROOT / 'scripts'), 'GITHUB_OUTPUT': str(tmp / 'output')},
        )
        output = (tmp / 'output').read_text() if (tmp / 'output').exists() else ''
        return result, output, marker.read_text() if marker.exists() else ''

    def test_one_dashboard_is_handed_on_alone(self):
        result, output, marker = self.listing(('grafana-dashboard.json',))
        self.assertEqual(
            (result.returncode, output, marker),
            (0, 'files=grafana-dashboard.json\n', ''),
            result.stderr,
        )

    def test_a_second_dashboard_is_handed_on_beside_the_first(self):
        result, output, marker = self.listing(
            ('grafana-dashboard-messages.json', 'grafana-dashboard.json')
        )
        self.assertEqual(
            (result.returncode, output, marker),
            (0, 'files=grafana-dashboard.json grafana-dashboard-messages.json\n', ''),
            result.stderr,
        )

    def test_a_refused_suffix_fails_the_job_and_checks_nothing(self):
        result, output, marker = self.listing(
            ('grafana-dashboard.json', 'grafana-dashboard-Messages.json')
        )
        self.assertEqual((result.returncode, output, marker), (1, '', 'List the dashboards\n'))
        self.assertIn('::error::grafana-dashboard-Messages.json cannot be published', result.stderr)


class CheckLoop(unittest.TestCase):
    """check.sh checks each file it is given against its own base copy."""

    def check(
        self, paths: str, fail: str = '', base_down: str = ''
    ) -> tuple[subprocess.CompletedProcess, list[str]]:
        tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        action = tmp / 'action'
        action.mkdir()
        shutil.copy(ACTION / 'check.sh', action / 'check.sh')
        log = tmp / 'log'
        (action / 'install.sh').write_text('touch "$1/cue" "$1/schema-v1.cue"\n')
        # Writes a base copy naming its path, except for BASE_DOWN, which fails as an unreadable base does.
        (action / 'fetch-base.sh').write_text(
            'rm -f "$3"\n'
            '[ "$1" != "${BASE_DOWN:-}" ] || exit 1\n'
            '[ -z "$2" ] || printf "base of %s" "$1" >"$3"\n'
        )
        (action / 'dashboard-check.py').write_text(
            'import os, sys\n'
            'args = sys.argv[1:]\n'
            'base = open(args[args.index("--base-file") + 1]).read() if "--base-file" in args else "none"\n'
            'with open(os.environ["LOG"], "a") as f:\n'
            '    f.write(f"{args[-1]} {base} {args.count(args[-1])}\\n")\n'
            'sys.exit(1 if args[-1] == os.environ.get("FAIL") else 0)\n'
        )
        tools = tmp / 'tools'
        tools.mkdir()
        result = run(
            ['bash', str(action / 'check.sh')],
            tmp,
            env={
                'DASHBOARD_PATH': paths,
                'BASE_REF': 'abc',
                'DASHBOARD_CHECK_TOOLS': str(tools),
                'LOG': str(log),
                'FAIL': fail,
                'BASE_DOWN': base_down,
            },
        )
        return result, log.read_text().splitlines() if log.exists() else []

    def test_one_path_is_checked_as_before(self):
        result, calls = self.check('grafana-dashboard.json')
        self.assertEqual(
            (result.returncode, calls),
            (0, ['grafana-dashboard.json base of grafana-dashboard.json 1']),
        )

    def test_each_path_is_checked_once_against_its_own_base(self):
        result, calls = self.check('grafana-dashboard.json grafana-dashboard-messages.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls,
            [
                'grafana-dashboard.json base of grafana-dashboard.json 1',
                'grafana-dashboard-messages.json base of grafana-dashboard-messages.json 1',
            ],
        )

    def test_a_failing_first_file_still_checks_the_second_and_fails(self):
        result, calls = self.check('a.json b.json', fail='a.json')
        self.assertEqual(result.returncode, 1)
        self.assertEqual([c.split()[0] for c in calls], ['a.json', 'b.json'])

    def test_an_unreadable_base_fails_but_checks_the_others(self):
        result, calls = self.check('a.json b.json', base_down='a.json')
        self.assertEqual((result.returncode, calls), (1, ['b.json base of b.json 1']))

    def test_no_path_is_an_error(self):
        result, calls = self.check('  ')
        self.assertEqual((result.returncode, calls), (2, []))
        self.assertIn('no dashboard file given', result.stderr)


if __name__ == '__main__':
    unittest.main()
