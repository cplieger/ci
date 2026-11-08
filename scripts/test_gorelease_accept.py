"""gorelease-accept.py against gorelease's report shape, the real web-terminal-engine report first."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent
SCRIPT = SCRIPTS / 'gorelease-accept.py'
TESTDATA = SCRIPTS / 'testdata' / 'gorelease-accept'
SPEC = importlib.util.spec_from_file_location('gorelease_accept', SCRIPT)
assert SPEC and SPEC.loader
ga = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ga)

ENGINE_REPORT = (TESTDATA / 'engine-v6.5.0.txt').read_text()
ENGINE_ACCEPT = (TESTDATA / 'engine-api-accept.txt').read_text()
TWO_PACKAGES = (TESTDATA / 'two-packages.txt').read_text()
TERMINAL = 'github.com/cplieger/web-terminal-engine/v6/terminal'
VT = 'github.com/cplieger/web-terminal-engine/v6/vt'
TWO_ACCEPT = (
    'example.com/lib/v2: Client.Close: changed from func() to func() error\n'
    'example.com/lib/v2/wire: Frame.Size: removed\n'
    'example.com/lib/v2/wire: Decode: removed\n'
    'example.com/lib/v2/wire/legacy: package removed\n'
)
PUBLISHED = 'v6.4.0 v6.5.0-dev.5'


def summary(version: str, *reason: str) -> str:
    return f'# summary\n{version} is not a valid semantic version for this release.\n' + ''.join(
        f'{line}\n' for line in reason
    )


class Cli(unittest.TestCase):
    """The script as the step runs it: exit code, annotations and the appended summary."""

    def run_cli(
        self,
        report: str,
        accept: str,
        *,
        verdict='api',
        base='v6.4.0',
        version='v6.5.0',
        lookups=('--existing', PUBLISHED, '--self', 'v6.5.0'),
    ):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ['rm', '-rf', str(tmp)], check=False)
        (tmp / 'report.txt').write_text(report)
        (tmp / 'api-accept.txt').write_text(accept)
        (tmp / 'summary.md').write_text('earlier\n')
        run = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                '--verdict',
                verdict,
                '--report',
                str(tmp / 'report.txt'),
                '--accept',
                str(tmp / 'api-accept.txt'),
                '--base',
                base,
                '--version',
                version,
                *lookups,
                '--summary',
                str(tmp / 'summary.md'),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return run, (tmp / 'summary.md').read_text()

    def test_the_engine_report_passes_with_the_engine_accept_file(self):
        run, md = self.run_cli(ENGINE_REPORT, ENGINE_ACCEPT)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn('::warning', run.stdout)
        self.assertTrue(md.startswith('earlier\n#### api-accept.txt\n'))
        self.assertIn(
            f'Accepted incompatible changes:\n\n```text\n{TERMINAL}: StartZombieReaper: removed\n```',
            md,
        )
        self.assertNotIn('Why the accepted list cannot pass', md)

    def test_the_engine_report_stays_rejected_with_only_comments(self):
        comments = ''.join(
            line + '\n' for line in ENGINE_ACCEPT.splitlines() if line.startswith('#')
        )
        run, md = self.run_cli(ENGINE_REPORT, comments)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn(
            f'Incompatible changes it does not accept:\n\n```text\n{TERMINAL}: StartZombieReaper: removed\n```',
            md,
        )
        self.assertNotIn('Accepted incompatible changes:', md)

    def test_a_fully_accepted_patch_bump_stays_rejected_with_its_reason(self):
        report = ENGINE_REPORT.split('# summary\n')[0] + summary(
            'v6.4.1', 'There are incompatible changes.'
        )
        run, md = self.run_cli(
            report,
            ENGINE_ACCEPT,
            version='v6.4.1',
            lookups=('--existing', PUBLISHED, '--self', 'v6.4.1'),
        )
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn(
            'Why the accepted list cannot pass the release:\n\n```text\n'
            'v6.4.1 does not increment the minor version over v6.4.0\n```',
            md,
        )

    def test_a_stale_entry_warns_at_its_line_and_does_not_block(self):
        accept = ENGINE_ACCEPT + '\n# kept by mistake\n' + f'{TERMINAL}: Session.Kill: removed\n'
        stale_line = len(accept.splitlines())
        run, md = self.run_cli(ENGINE_REPORT, accept)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn(
            f'::warning file=api-accept.txt,line={stale_line}::{TERMINAL}: Session.Kill: removed',
            run.stdout,
        )
        self.assertIn(f'line {stale_line}: {TERMINAL}: Session.Kill: removed', md)

    def test_an_ok_verdict_only_warns_about_every_entry(self):
        report = f'# {VT}\n## compatible changes\nScreen.Reset: added\n\n# summary\nv6.6.0 is a valid semantic version for this release.\n'
        run, md = self.run_cli(report, ENGINE_ACCEPT, verdict='ok', base='v6.5.0', version='v6.6.0')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('::warning file=api-accept.txt,line=4::', run.stdout)
        self.assertIn('Entries that match no incompatible change:', md)
        self.assertNotIn('Accepted incompatible changes:', md)

    def test_a_malformed_entry_exits_2_naming_its_line(self):
        for line in (
            'StartZombieReaper removed',
            f'{TERMINAL}:',
            f'{TERMINAL} x: StartZombieReaper: removed',
            f'{TERMINAL}:StartZombieReaper: removed',
        ):
            with self.subTest(line=line):
                run, md = self.run_cli(ENGINE_REPORT, f'# c\n\n{line}\n')
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertIn('::error::api-accept.txt line 3 is not', run.stdout)
                self.assertEqual(md, 'earlier\n')

    def test_a_non_stable_version_argument_exits_2(self):
        run, _ = self.run_cli(ENGINE_REPORT, ENGINE_ACCEPT, version='v6.5.0-dev.1')
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)

    def test_a_listed_version_that_is_not_semver_exits_2(self):
        for flag, value, bad in (
            ('--existing', 'v6.4.0 latest', 'latest'),
            ('--self', 'latest', 'latest'),
            ('--self', '', ''),
        ):
            with self.subTest(flag=flag, value=value):
                run, _ = self.run_cli(ENGINE_REPORT, ENGINE_ACCEPT, lookups=(flag, value))
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertIn(f'::error::{bad!r} is not a semantic version', run.stdout)

    def test_omitted_lookups_block_acceptance(self):
        run, md = self.run_cli(ENGINE_REPORT, ENGINE_ACCEPT, lookups=())
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn(
            'Why the accepted list cannot pass the release:\n\n```text\n'
            'the published versions could not be listed (go list -m -versions)\n'
            'the version a dependent module selects could not be listed (go list -m)\n```',
            md,
        )

    def test_an_unreadable_report_rejects_under_api_and_warns_under_ok(self):
        report = 'go: downloading example.com/x v1.0.0\n' + ENGINE_REPORT
        run, md = self.run_cli(report, ENGINE_ACCEPT)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn('::error::cannot read the gorelease report', run.stdout)
        self.assertIn("line outside any section: 'go: downloading example.com/x v1.0.0'", md)
        run, _ = self.run_cli(report, ENGINE_ACCEPT, verdict='ok')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('::warning::cannot read the gorelease report', run.stdout)
        self.assertNotIn('file=api-accept.txt', run.stdout)


class Matching(unittest.TestCase):
    """An entry accepts one report line of one package, compared whole."""

    def apply(self, report: str, accept: str, base='v6.4.0', version='v6.5.0'):
        return ga.apply(
            ga.parse_report(report), ga.parse_accept(accept), base, version, [], version
        )

    def test_the_real_report_parses_into_its_two_packages(self):
        report = ga.parse_report(ENGINE_REPORT)
        self.assertEqual(report.packages[TERMINAL].incompatible, ['StartZombieReaper: removed'])
        self.assertEqual(report.packages[VT].compatible, ['Screen.FocusEnableSeq: added'])
        self.assertEqual(report.diagnostics, [])
        self.assertEqual(
            report.summary,
            [
                'v6.5.0 is not a valid semantic version for this release.',
                'There are incompatible changes.',
            ],
        )

    def test_no_partial_or_cross_package_entry_accepts_a_line(self):
        for entry in (
            f'{VT}: StartZombieReaper: removed',
            'github.com/cplieger/web-terminal-engine/v6: StartZombieReaper: removed',
            f'{TERMINAL}/sub: StartZombieReaper: removed',
            f'{TERMINAL}: StartZombieReaper',
            f'{TERMINAL}: removed',
            f'{TERMINAL}: StartZombieReaper: removed and more',
            f'{VT}: Screen.FocusEnableSeq: added',
        ):
            with self.subTest(entry=entry):
                result = self.apply(ENGINE_REPORT, entry + '\n')
                self.assertEqual(result.accepted, [])
                self.assertEqual([e.change for e in result.stale], [entry.partition(': ')[2]])
                self.assertEqual(result.unaccepted, [f'{TERMINAL}: StartZombieReaper: removed'])

    def test_two_packages_pass_when_every_line_is_accepted(self):
        result = self.apply(TWO_PACKAGES, TWO_ACCEPT, base='v2.3.1', version='v2.4.0')
        self.assertEqual(len(result.accepted), 4)
        self.assertEqual((result.stale, result.unaccepted, result.reasons), ([], [], []))

    def test_an_unaccepted_second_removal_keeps_the_rejection(self):
        accept = TWO_ACCEPT.replace('example.com/lib/v2/wire: Decode: removed\n', '')
        result = self.apply(TWO_PACKAGES, accept, base='v2.3.1', version='v2.4.0')
        self.assertEqual(result.unaccepted, ['example.com/lib/v2/wire: Decode: removed'])
        self.assertEqual(result.reasons, [])

    def test_comments_blank_lines_and_crlf_are_skipped_and_line_numbers_kept(self):
        accept = (
            f'\r\n  # indented comment\r\n\r\n\t\r\n  {TERMINAL}: StartZombieReaper: removed  \r\n'
        )
        (entry,) = ga.parse_accept(accept)
        self.assertEqual(
            (entry.lineno, entry.package, entry.change), (5, TERMINAL, 'StartZombieReaper: removed')
        )
        self.assertEqual(self.apply(ENGINE_REPORT, accept).unaccepted, [])

    def test_a_utf8_bom_is_ignored_before_an_entry_or_a_comment(self):
        line = f'{TERMINAL}: StartZombieReaper: removed\n'
        (entry,) = ga.parse_accept('\ufeff' + line)
        self.assertEqual((entry.lineno, entry.package), (1, TERMINAL))
        self.assertEqual(ga.parse_accept('\ufeff# a comment\n'), [])

    def run_quiet(self, accept: str):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code, md = ga.run('api', ENGINE_REPORT, accept, 'v6.4.0', 'v6.5.0', PUBLISHED, 'v6.5.0')
        return code, '\n'.join(md), out.getvalue()

    def test_a_duplicate_entry_matches_once_and_warns(self):
        line = f'{TERMINAL}: StartZombieReaper: removed\n'
        code, md, out = self.run_quiet(line + line)
        self.assertEqual(code, 0)
        self.assertEqual(out, f'::warning file=api-accept.txt,line=2::duplicate of line 1: {line}')
        self.assertEqual(md.count('StartZombieReaper: removed'), 1)

    def test_a_repeated_stale_entry_warns_once_as_stale_and_once_as_a_duplicate(self):
        line = f'{TERMINAL}: StartZombieReaper: removed\n'
        stale = f'{TERMINAL}: Session.Kill: removed'
        code, md, out = self.run_quiet(f'{line}{stale}\n{stale}\n')
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                f'::warning file=api-accept.txt,line=3::duplicate of line 2: {stale}',
                (
                    f'::warning file=api-accept.txt,line=2::{stale} matches no incompatible change '
                    'gorelease reported; delete the entry'
                ),
            ],
        )
        self.assertEqual(md.count('Session.Kill: removed'), 1)


class Recompute(unittest.TestCase):
    """Accepting every line passes only when the incompatible changes were the whole verdict."""

    def reasons(
        self,
        report: str,
        accept=ENGINE_ACCEPT,
        base='v6.4.0',
        version='v6.5.0',
        existing=('v6.4.0',),
        self_version='v6.5.0',
    ):
        result = ga.apply(
            ga.parse_report(report),
            ga.parse_accept(accept),
            base,
            version,
            None if existing is None else list(existing),
            self_version,
        )
        self.assertEqual(result.unaccepted, [])
        return result.reasons

    def test_diagnostics_block_acceptance(self):
        report = ENGINE_REPORT.replace(
            '# summary\n',
            "# diagnostics\ngo.sum: one or more sums are missing. Run 'go mod tidy' to add missing sums.\n\n# summary\n",
        )
        self.assertEqual(self.reasons(report), ['gorelease reported diagnostics'])

    def test_a_base_load_error_blocks_acceptance(self):
        report = ENGINE_REPORT.replace(
            f'# {VT}\n',
            f'# {VT}\n## errors in base version:\nvt/screen.go:1:1: expected package\n\n',
        )
        self.assertEqual(self.reasons(report), [f'{VT} has package load errors'])

    def test_another_rejection_reason_blocks_acceptance(self):
        report = ENGINE_REPORT.split('# summary\n')[0] + summary(
            'v6.5.0',
            'The major version v6 does not match the major version suffix',
            'in the module path: example.com/m',
        )
        self.assertEqual(
            self.reasons(report),
            ['gorelease rejected the version for a reason other than its changes'],
        )

    def test_a_published_version_blocks_acceptance(self):
        self.assertEqual(
            self.reasons(ENGINE_REPORT, existing=('v6.4.0', 'v6.5.0')),
            ['version v6.5.0 already exists'],
        )
        self.assertEqual(self.reasons(ENGINE_REPORT, existing=('v6.5.0-dev.5',)), [])

    def test_a_higher_self_dependency_blocks_acceptance(self):
        self.assertEqual(
            self.reasons(ENGINE_REPORT, self_version='v6.5.1-dev.1'),
            ['the module indirectly depends on a higher version of itself (v6.5.1-dev.1)'],
        )
        self.assertEqual(self.reasons(ENGINE_REPORT, self_version='v6.5.0+meta'), [])

    def test_an_omitted_lookup_blocks_acceptance(self):
        self.assertEqual(
            self.reasons(ENGINE_REPORT, existing=None, self_version=None),
            [
                'the published versions could not be listed (go list -m -versions)',
                'the version a dependent module selects could not be listed (go list -m)',
            ],
        )

    def test_a_patch_bump_blocks_acceptance(self):
        report = ENGINE_REPORT.split('# summary\n')[0] + summary(
            'v6.4.1', 'There are incompatible changes.'
        )
        self.assertEqual(
            self.reasons(report, version='v6.4.1', self_version='v6.4.1'),
            ['v6.4.1 does not increment the minor version over v6.4.0'],
        )

    def test_an_unknown_report_shape_is_refused(self):
        for report in (
            'stray\n' + ENGINE_REPORT,
            ENGINE_REPORT.replace('## incompatible changes', '## removed things'),
            ENGINE_REPORT.replace(f'# {VT}\n', f'# {TERMINAL}\n'),
            '## incompatible changes\nX: removed\n',
        ):
            with self.subTest(report=report[:40]), self.assertRaises(ga.ReportError):
                ga.parse_report(report)


class SemverOrder(unittest.TestCase):
    def test_versions_sort_in_semver_precedence(self):
        ordered = [
            'v6.4.0',
            'v6.5.0-dev.1',
            'v6.5.0-dev.2',
            'v6.5.0-dev.10',
            'v6.5.0-dev.alpha',
            'v6.5.0',
            'v6.5.1',
            'v6.10.0',
        ]
        self.assertEqual(sorted(ordered[1::2] + ordered[::2], key=ga.semver_key), ordered)
        self.assertEqual(ga.semver_key('v2.0.0+incompatible'), ga.semver_key('v2.0.0'))


class Escape(unittest.TestCase):
    def test_a_workflow_command_message_cannot_break_out_of_its_line(self):
        self.assertEqual(ga.escape('100%\r\n::error::x'), '100%25%0D%0A::error::x')


if __name__ == '__main__':
    unittest.main()
