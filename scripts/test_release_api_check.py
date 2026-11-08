"""Tests for release.yaml's API compatibility step: its skip paths and verdicts, run as the runner runs them."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

RELEASE = Path(__file__).resolve().parent.parent / '.github/workflows/release.yaml'
STEP = 'API compatibility (gorelease)'


def step_body() -> str:
    for step in yaml.safe_load(RELEASE.read_text())['jobs']['go']['steps']:
        if step.get('name') == STEP:
            return re.sub(r'\$\{\{[^}]*\}\}', '', step['run'])
    raise AssertionError(f'release.yaml go has no step named {STEP!r}')


GIT_ENV = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


def git(repo: Path, *args: str) -> None:
    subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, env=GIT_ENV)


class SkipPaths(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ['rm', '-rf', str(self.repo)], check=False)
        git(self.repo, 'init', '-q')
        (self.repo / 'go.mod').write_text('module example.com/lib\n')
        git(self.repo, 'add', 'go.mod')
        git(
            self.repo,
            '-c',
            'user.name=t',
            '-c',
            'user.email=t@example.com',
            'commit',
            '-qm',
            'init',
        )

    def run_step(self, version: str) -> subprocess.CompletedProcess:
        # GitHub runs a run block as `bash -e {0}`, so -e is part of the contract under test.
        return subprocess.run(
            ['bash', '-e', '-c', step_body()],
            cwd=self.repo,
            env={**os.environ, 'VERSION': version},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_a_first_release_with_no_stable_tag_skips(self):
        git(self.repo, 'tag', 'v1.0.0-dev.1')
        run = self.run_step('v1.0.0')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('no previous stable tag', run.stdout)

    def test_a_major_bump_skips_after_finding_the_previous_tag(self):
        git(self.repo, 'tag', 'v1.2.0')
        git(self.repo, 'tag', 'v1.10.0')
        run = self.run_step('v2.0.0')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('major bump v1.10.0 -> v2.0.0', run.stdout)


TESTDATA = Path(__file__).resolve().parent / 'testdata' / 'gorelease-accept'
ENGINE_REPORT = (TESTDATA / 'engine-v6.5.0.txt').read_text()
ENGINE_ACCEPT = (TESTDATA / 'engine-api-accept.txt').read_text()
OK_REPORT = (
    '# example.com/lib/v6/vt\n## compatible changes\nScreen.Reset: added\n\n'
    '# summary\nv6.5.0 is a valid semantic version for this release.\n'
)
GO = shutil.which('go')
# gorelease's install, GOPATH and the proxy's version list are stubbed. Each module command must
# run where and as the step runs it, and real go answers it against a file proxy.
GO_STUB = """#!/bin/bash
self_dir() { case "$PWD" in "$RUNNER_TEMP"/gorelease-self.*) ;; *) exit 98 ;; esac; }
case "$*" in
  'install golang.org/x/exp/cmd/gorelease@'*) exit 0 ;;
  'env GOPATH') echo "$STUB_GOPATH" && exit 0 ;;
  'list -m -versions -f {{join .Versions " "}} example.com/lib/v6')
    [ "$PWD" = "$RUNNER_TEMP" ] && [ -z "$STUB_LIST_FAIL" ] || exit 1
    echo "$STUB_VERSIONS" && exit 0
    ;;
  'mod edit -json') [ "$PWD" = "$STUB_REPO" ] || exit 98 ;;
  'mod edit -require=example.com/lib/v6@v6.5.0 -replace=example.com/lib/v6='"$STUB_REPO"' -go=1.22 -require=example.com/dep@v1.'?'.0') self_dir ;;
  'list -mod=mod -m all') self_dir && [ -z "$STUB_SELF_FAIL" ] || exit 1 ;;
  'list -m -f {{.Version}} -- example.com/lib/v6') self_dir ;;
  *) echo "unexpected: go $*" >&2 && exit 97 ;;
esac
exec "$REAL_GO" "$@"
"""
GORELEASE_STUB = """#!/bin/bash
cat "$STUB_REPORT"
exit "$STUB_RC"
"""
# example.com/dep v1.0.0 requires the module below the release version, v1.1.0 above it.
PROXY = {
    'example.com/lib/v6': {'v6.4.0': '', 'v6.6.0-dev.1': ''},
    'example.com/dep': {
        'v1.0.0': 'require example.com/lib/v6 v6.4.0\n',
        'v1.1.0': 'require example.com/lib/v6 v6.6.0-dev.1\n',
    },
}
HIGHER = 'the module indirectly depends on a higher version of itself (v6.6.0-dev.1)'


# CI must run these, so a runner without Go fails them instead of skipping.
@unittest.skipUnless(GO or os.environ.get('CI'), 'go is not installed')
class Verdicts(unittest.TestCase):
    """The step after gorelease ran, with gorelease stubbed and api-accept.txt in the caller's tree."""

    def setUp(self):
        self.assertTrue(GO, 'go is not on PATH')
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(subprocess.run, ['rm', '-rf', str(self.tmp)], check=False)
        self.addCleanup(subprocess.run, ['chmod', '-R', 'u+w', str(self.tmp)], check=False)
        for path, versions in PROXY.items():
            d = self.tmp / 'proxy' / path / '@v'
            d.mkdir(parents=True)
            (d / 'list').write_text(''.join(f'{v}\n' for v in versions))
            for v, requires in versions.items():
                (d / f'{v}.info').write_text(f'{{"Version":"{v}"}}')
                (d / f'{v}.mod').write_text(f'module {path}\n\ngo 1.22\n\n{requires}')
        self.go_env = {
            'GOPROXY': f'file://{self.tmp / "proxy"}',
            'GOSUMDB': 'off',
            'GOFLAGS': '',
            'GONOPROXY': '',
            'GONOSUMDB': '',
            'GOPRIVATE': '',
            'GOMODCACHE': str(self.tmp / 'modcache'),
            'GOTOOLCHAIN': 'local',
            'GOENV': 'off',
            'GOWORK': 'off',
        }
        self.repo = self.tmp / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        (self.repo / 'lib.go').write_text('package lib\n')
        self.go_mod('require example.com/dep v1.0.0\n')
        git(self.repo, 'add', 'go.mod', 'go.sum', 'lib.go')
        git(
            self.repo,
            '-c',
            'user.name=t',
            '-c',
            'user.email=t@example.com',
            'commit',
            '-qm',
            'init',
        )
        git(self.repo, 'tag', 'v6.4.0')
        self.stub = self.tmp / 'stub'
        (self.stub / 'gopath' / 'bin').mkdir(parents=True)
        for path, body in (
            (self.stub / 'go', GO_STUB),
            (self.stub / 'gopath/bin/gorelease', GORELEASE_STUB),
        ):
            path.write_text(body)
            path.chmod(0o755)
        self.runner_temp = self.tmp / 'runner'
        self.runner_temp.mkdir()
        self.summary = self.tmp / 'summary.md'

    def go_mod(self, directives: str) -> None:
        """Write the caller's go.mod and fill its go.sum as the caller's own build does."""
        (self.repo / 'go.mod').write_text(f'module example.com/lib/v6\n\ngo 1.22\n\n{directives}')
        subprocess.run(
            [GO, 'list', '-mod=mod', '-m', 'all'],
            cwd=self.repo,
            env={**os.environ, **self.go_env},
            check=True,
            capture_output=True,
            timeout=60,
        )

    def run_step(self, report: str, rc: int, accept: str | None, **stub: str):
        (self.tmp / 'report.txt').write_text(report)
        if accept is not None:
            (self.repo / 'api-accept.txt').write_text(accept)
        run = subprocess.run(
            ['bash', '-e', '-c', step_body()],
            cwd=self.repo,
            env={
                **os.environ,
                **self.go_env,
                'PATH': f'{self.stub}{os.pathsep}{os.environ["PATH"]}',
                'VERSION': 'v6.5.0',
                'CI_TOOLS': str(RELEASE.parent.parent.parent / 'scripts'),
                'RUNNER_TEMP': str(self.runner_temp),
                'GITHUB_STEP_SUMMARY': str(self.summary),
                'REAL_GO': GO,
                'STUB_REPO': str(self.repo),
                'STUB_GOPATH': str(self.stub / 'gopath'),
                'STUB_REPORT': str(self.tmp / 'report.txt'),
                'STUB_RC': str(rc),
                'STUB_VERSIONS': 'v6.3.0 v6.4.0 v6.5.0-dev.5',
                'STUB_LIST_FAIL': '',
                'STUB_SELF_FAIL': '',
                **stub,
            },
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertNotIn('unexpected: go', run.stderr)
        return run, self.summary.read_text()

    def test_without_the_file_an_incompatible_change_fails_as_before(self):
        run, md = self.run_step(ENGINE_REPORT, 1, None)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn('::error::gorelease rejected v6.5.0', run.stdout)
        self.assertNotIn('#### api-accept.txt', md)
        self.assertFalse((self.runner_temp / 'api-accept.md').exists())

    def test_the_engine_accept_file_passes_the_engine_report(self):
        run, md = self.run_step(ENGINE_REPORT, 1, ENGINE_ACCEPT)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn(
            '::notice::every incompatible change gorelease reported for v6.5.0 is in api-accept.txt',
            run.stdout,
        )
        self.assertIn('verdict: accepted', run.stdout)
        self.assertIn("repository's `api-accept.txt` accepts, so the release proceeds.", md)
        self.assertIn('Accepted incompatible changes:', md)
        self.assertIn(ENGINE_REPORT.splitlines()[0], md)

    def assert_blocked(self, reason: str, **stub: str):
        run, md = self.run_step(ENGINE_REPORT, 1, ENGINE_ACCEPT, **stub)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn('::error::gorelease rejected v6.5.0', run.stdout)
        self.assertIn(f'Why the accepted list cannot pass the release:\n\n```text\n{reason}\n', md)

    def test_a_published_version_or_a_failed_lookup_blocks_the_accept_file(self):
        for stub, reason in (
            ({'STUB_VERSIONS': 'v6.4.0 v6.5.0'}, 'version v6.5.0 already exists'),
            (
                {'STUB_LIST_FAIL': '1'},
                'the published versions could not be listed (go list -m -versions)',
            ),
            (
                {'STUB_SELF_FAIL': '1'},
                'the version a dependent module selects could not be listed (go list -m)',
            ),
        ):
            with self.subTest(stub=stub):
                self.assert_blocked(reason, **stub)
                self.summary.unlink()
                (self.runner_temp / 'api-accept.md').unlink()

    def test_a_dependency_requiring_a_higher_version_blocks_through_replace_and_exclude(self):
        for directives in (
            'require example.com/dep v1.1.0\n',
            'require example.com/dep v1.1.0\n\nreplace example.com/dep v1.1.0 => example.com/dep v1.0.0\n',
            'require example.com/dep v1.1.0\n\nexclude example.com/lib/v6 v6.6.0-dev.1\n',
        ):
            with self.subTest(directives=directives):
                self.go_mod(directives)
                self.assert_blocked(HIGHER)
                self.summary.unlink()
                (self.runner_temp / 'api-accept.md').unlink()

    def test_an_entry_for_another_change_leaves_the_release_rejected(self):
        accept = 'github.com/cplieger/web-terminal-engine/v6/terminal: StartReaper: removed\n'
        run, md = self.run_step(ENGINE_REPORT, 1, accept)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn('::warning file=api-accept.txt,line=1::', run.stdout)
        self.assertIn('::error::gorelease rejected v6.5.0', run.stdout)
        self.assertIn('Incompatible changes it does not accept:', md)

    def test_an_ok_verdict_warns_about_a_stale_entry_and_passes(self):
        run, md = self.run_step(OK_REPORT, 0, ENGINE_ACCEPT)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('::warning file=api-accept.txt,line=4::', run.stdout)
        self.assertIn('Entries that match no incompatible change:', md)

    def test_a_malformed_file_fails_even_an_ok_verdict(self):
        run, md = self.run_step(OK_REPORT, 0, 'StartZombieReaper removed\n')
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn('::error::api-accept.txt line 1 is not', run.stdout)
        self.assertIn('::error::api-accept.txt could not be applied', run.stdout)
        self.assertIn('could not be applied, and this FAILS the release.', md)

    def test_a_load_error_stays_an_operational_pass_and_never_reads_the_file(self):
        report = ENGINE_REPORT.replace(
            'There are incompatible changes.', 'Errors were found in one or more packages.'
        )
        run, md = self.run_step(report, 1, ENGINE_ACCEPT)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('::warning::gorelease could not run', run.stdout)
        self.assertNotIn('#### api-accept.txt', md)


if __name__ == '__main__':
    unittest.main()
