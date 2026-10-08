"""Tests for backfill-release-notes.py: its renderers (fake gh and git-cliff seams),
its carve-out list and its pair selection."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
BACKFILL = SCRIPTS / 'backfill-release-notes.py'
CONFIG = SCRIPTS.parent / 'configs' / 'cliff-stable.toml'
GIT_ENV = {
    'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_AUTHOR_NAME': 'probe',
    'GIT_AUTHOR_EMAIL': 'probe@ci.local',
    'GIT_COMMITTER_NAME': 'probe',
    'GIT_COMMITTER_EMAIL': 'probe@ci.local',
}

# Answers the REST calls the backfill makes (`gh api -i`), from files under
# FAKE_GH_DIR; an edited body is stored there and read back by the next read.
# An asset read without `Accept: application/octet-stream` answers the asset's
# JSON metadata, as GitHub does. FAKE_FULL_PAGES answers every release page full.
FAKE_GH = """\
#!/usr/bin/env python3
import json, os, sys, urllib.parse
d = os.environ['FAKE_GH_DIR']
args = sys.argv[1:]
stdin = sys.stdin.read() if '--input' in args else ''
with open(os.path.join(d, 'gh.argv'), 'a', encoding='utf-8') as f:
    f.write(json.dumps(args) + '\\n')
tags = os.environ.get('FAKE_TAGS', 'v1.0.0 v1.1.0').split()
drafts = os.environ.get('FAKE_DRAFTS', '').split()
def answer(status, body):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    sys.stdout.buffer.write(f'HTTP/2.0 {status} X\\r\\n\\r\\n'.encode() + raw)
    sys.exit(0 if status < 300 else 1)
def assets(tag):
    names = ['sbom.spdx.json', 'sbom.spdx.json.sigstore.json']
    return [{'id': f'{tag}--{n}', 'name': n} for n in names
            if os.path.exists(os.path.join(d, tag + n.removeprefix('sbom')))]
if args[:2] == ['api', '-i']:
    method, headers, path, i = 'GET', [], None, 2
    while i < len(args):
        if args[i] in ('-X', '-H', '--input'):
            method = args[i + 1] if args[i] == '-X' else method
            headers += [args[i + 1]] if args[i] == '-H' else []
            i += 2
        else:
            path, i = args[i], i + 1
    where, _, query = path.partition('?')
    q = dict(urllib.parse.parse_qsl(query))
    if (method, where) == ('GET', 'repos/owner/app'):
        answer(200, {'full_name': 'owner/app'})
    if (method, where) == ('GET', 'repos/owner/app/releases'):
        per = int(q['per_page'])
        if os.environ.get('FAKE_FULL_PAGES'):
            answer(200, [{'tag_name': f'v0.0.{n}', 'draft': False, 'prerelease': False}
                         for n in range(per)])
        rows = [{'tag_name': t, 'draft': t in drafts, 'prerelease': '-' in t}
                for t in tags + drafts]
        page = int(q['page'])
        answer(200, rows[(page - 1) * per : page * per])
    if method == 'GET' and where.startswith('repos/owner/app/releases/tags/'):
        tag = urllib.parse.unquote(where.rsplit('/', 1)[1])
        if tag not in tags:
            answer(404, {'message': 'Not Found'})
        edited = os.path.join(d, tag + '.edited.md')
        body = open(edited, encoding='utf-8', newline='').read() if os.path.exists(edited) else 'old body'
        answer(200, {'id': 1000 + tags.index(tag), 'tag_name': tag, 'body': body, 'assets': assets(tag)})
    if method == 'GET' and where.startswith('repos/owner/app/releases/assets/'):
        tag, _, name = where.rsplit('/', 1)[1].partition('--')
        if 'Accept: application/octet-stream' not in headers:
            answer(200, {'id': f'{tag}--{name}', 'name': name})
        answer(200, open(os.path.join(d, tag + name.removeprefix('sbom')), 'rb').read())
    if method == 'PATCH' and where.startswith('repos/owner/app/releases/'):
        tag = tags[int(where.rsplit('/', 1)[1]) - 1000]
        with open(os.path.join(d, tag + '.edited.md'), 'w', encoding='utf-8', newline='') as f:
            f.write(json.loads(stdin)['body'])
        answer(200, {})
    answer(404, {'message': f'fake gh: no route for {method} {path}'})
elif args[:1] == ['api'] and '/pulls?' in args[1]:
    with open(os.path.join(d, 'gh-api.argv'), 'a', encoding='utf-8') as f:
        f.write(args[1] + '\\n')
    base = args[1].split('&base=')[1].split('&')[0]
    pulls = os.path.join(d, f'pulls-{base}.json')
    first = args[1].endswith('&page=1') and os.path.exists(pulls)
    print(open(pulls, encoding='utf-8').read() if first else '[]')
else:
    sys.exit(f'fake gh: unexpected {args}')
"""

# A bundle here records the sha256 of the bytes it signed and the repository
# and commit of the run that signed them; verify-blob passes exactly when the
# asset is those bytes and every certificate constraint it was given holds.
FAKE_COSIGN = """\
#!/usr/bin/env python3
import hashlib, json, os, sys
args = sys.argv[1:]
with open(os.path.join(os.environ['FAKE_GH_DIR'], 'cosign.argv'), 'a', encoding='utf-8') as f:
    f.write(' '.join(args) + '\\n')
if args[:1] != ['verify-blob']:
    sys.exit(f'fake cosign: unexpected {args}')
bundle = json.load(open(args[args.index('--bundle') + 1], encoding='utf-8'))
for flag, key in (('--certificate-github-workflow-repository', 'repository'),
                  ('--certificate-github-workflow-sha', 'sha')):
    if flag in args and args[args.index(flag) + 1] != bundle[key]:
        sys.exit('fake cosign: none of the expected identities matched')
sys.exit(0 if hashlib.sha256(open(args[-1], 'rb').read()).hexdigest() == bundle['sha256'] else 'fake cosign: no match')
"""

# Records its argv and prints FAKE_CLIFF_OUT, standing in for the pinned binary.
FAKE_CLIFF = """\
#!/bin/sh
printf '%s\\n' "$*" >>"$FAKE_GH_DIR/cliff.argv"
printf '%s' "$FAKE_CLIFF_OUT"
"""


def sbom(packages: dict[str, str]) -> str:
    return json.dumps(
        {
            'documentDescribes': ['SPDXRef-DocumentRoot-Image'],
            'packages': [
                {'SPDXID': 'SPDXRef-DocumentRoot-Image', 'name': 'app', 'versionInfo': 'sha256:1'},
                *(
                    {'SPDXID': f'SPDXRef-{n}', 'name': n, 'versionInfo': v}
                    for n, v in packages.items()
                ),
            ],
        }
    )


class BackfillRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='backfill-test-'))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.env = {**os.environ, **GIT_ENV, 'FAKE_GH_DIR': str(self.tmp)}
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir()
        for name, body in (('gh', FAKE_GH), ('git-cliff', FAKE_CLIFF), ('cosign', FAKE_COSIGN)):
            (bin_dir / name).write_text(body, encoding='utf-8')
            (bin_dir / name).chmod(0o755)
        self.env['PATH'] = f'{bin_dir}{os.pathsep}{self.env["PATH"]}'
        self.cliff = str(bin_dir / 'git-cliff')
        self.repo = self.tmp / 'repo'

    def git(self, *args: str) -> str:
        return subprocess.run(
            ['git', '-C', str(self.repo), *args],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, files: dict[str, str], message: str) -> None:
        for path, text in files.items():
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text(text, encoding='utf-8')
        self.git('add', '-A')
        self.git('commit', '-qm', message)

    def build(self, *, bump_dep: bool, third: bool = False, bump_both: bool = False) -> None:
        """v1.0.0, then a root fix, a lane feature and maybe a dependency bump at
        v1.1.0 (with `bump_both`, a second dependency's bump after it), and with
        `third` a second fix at v1.2.0."""
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        gomod = (
            'module example.com/app\n\ngo 1.26\n\nrequire (\n'
            '\texample.com/dep {}\n\texample.com/other {}\n)\n'
        )
        self.commit(
            {
                'go.mod': gomod.format('v1.0.0', 'v1.0.0'),
                'main.go': 'package main\n',
                'Dockerfile': 'FROM scratch\n',
                'yamlenv/go.mod': 'module example.com/app/yamlenv\n\ngo 1.26\n',
                'yamlenv/y.go': 'package yamlenv\n',
            },
            'feat: initial',
        )
        self.git('tag', 'v1.0.0')
        self.commit({'main.go': 'package main\n\n// fix\n'}, 'fix: root fix (#2)')
        self.commit({'yamlenv/y.go': 'package yamlenv\n\n// f\n'}, 'feat: lane feature (#3)')
        if bump_dep:
            self.commit({'go.mod': gomod.format('v1.1.0', 'v1.0.0')}, 'fix(deps): bump dep (#4)')
        if bump_both:
            self.commit({'go.mod': gomod.format('v1.1.0', 'v1.2.0')}, 'fix(deps): bump other (#6)')
        self.git('tag', 'v1.1.0')
        if third:
            self.commit({'main.go': 'package main\n\n// fix 2\n'}, 'fix: second fix (#5)')
            self.git('tag', 'v1.2.0')
        subprocess.run(
            ['git', 'clone', '-q', '--bare', str(self.repo), str(self.tmp / 'origin.git')],
            env=self.env,
            check=True,
        )
        # origin names the GitHub repository; git reaches the local bare copy instead.
        self.git('remote', 'add', 'origin', 'https://github.com/owner/app.git')
        self.git(
            'config', f'url.{self.tmp / "origin.git"}.insteadOf', 'https://github.com/owner/app.git'
        )

    def backfill(self, *args: str, cliff_out: str, **env: str) -> str:
        proc = self.backfill_proc(*args, cliff_out=cliff_out, **env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def backfill_proc(self, *args: str, cliff_out: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(BACKFILL),
                '--repo-dir',
                str(self.repo),
                '--config',
                str(CONFIG),
                '--cliff-bin',
                self.cliff,
                *args,
            ],
            env={**self.env, 'FAKE_CLIFF_OUT': cliff_out, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def cliff_argv(self) -> str:
        return (self.tmp / 'cliff.argv').read_text(encoding='utf-8')

    def release_sbom(
        self,
        tag: str,
        packages: dict[str, str],
        *,
        shipped=None,
        repo: str = 'owner/app',
        signed_at: str | None = None,
    ) -> None:
        """A release's SBOM asset and the bundle a run of `repo` at `signed_at`
        (default the tag's commit) made over `packages`; `shipped` replaces the
        asset's content after signing."""
        signed = sbom(packages)
        (self.tmp / f'{tag}.spdx.json').write_text(sbom(shipped or packages), encoding='utf-8')
        bundle = {
            'sha256': hashlib.sha256(signed.encode('utf-8')).hexdigest(),
            'repository': repo,
            'sha': signed_at or self.git('rev-parse', f'{tag}^{{commit}}'),
        }
        (self.tmp / f'{tag}.spdx.json.sigstore.json').write_text(
            json.dumps(bundle), encoding='utf-8'
        )

    def test_legacy_is_the_default_and_excludes_lane_commits(self):
        self.build(bump_dep=True)
        out = self.backfill(cliff_out='### Fixed\n\n- Root fix (#2)\n')
        self.assertIn('+### Fixed', out)
        self.assertNotIn('Full changelog', out)
        argv = self.cliff_argv()
        self.assertIn('--exclude-path yamlenv/**', argv)
        self.assertIn('v1.0.0..v1.1.0', argv)
        self.assertNotIn('--tag v1.1.0', argv)

    def test_two_branch_renders_through_render_notes_with_both_sboms(self):
        self.build(bump_dep=True)
        self.release_sbom('v1.0.0', {'busybox': '1.0'})
        self.release_sbom('v1.1.0', {'busybox': '1.1'})
        out = self.backfill(
            '--release-model',
            'two-branch',
            cliff_out='### Fixed\n\n- Root fix (#2)\n',
            GH_TOKEN='a-token-render-notes-must-never-see',
        )
        for line in (
            '+### Fixed',
            '+**Full changelog**: https://github.com/owner/app/compare/v1.0.0...v1.1.0',
            '+- `example.com/dep` v1.0.0 to v1.1.0 (Go)',
            '+<summary>System packages: 1 change (busybox)</summary>',
            '+- `busybox` 1.0 to 1.1',
        ):
            self.assertIn(line, out)
        argv = self.cliff_argv()
        self.assertIn(
            r'--tag-pattern ^v[0-9]+\.[0-9]+\.[0-9]+$ --tag v1.1.0 --exclude-path yamlenv/**', argv
        )
        self.assertIn(f'v1.0.0..{self.git("rev-parse", "v1.1.0")}', argv)
        verified = (self.tmp / 'cosign.argv').read_text(encoding='utf-8')
        self.assertEqual(verified.count('verify-blob --bundle '), 2)
        self.assertIn(
            '--certificate-oidc-issuer https://token.actions.githubusercontent.com '
            r'--certificate-identity-regexp ^https://github\.com/cplieger/ci/\.github/workflows/docker-release\.yaml@ ',
            verified,
        )
        for tag in ('v1.0.0', 'v1.1.0'):
            self.assertIn(
                '--certificate-github-workflow-repository owner/app '
                f'--certificate-github-workflow-sha {self.git("rev-parse", tag)} ',
                verified,
            )

    def test_two_branch_refuses_an_sbom_signed_for_another_repository(self):
        self.build(bump_dep=True)
        self.release_sbom('v1.0.0', {'busybox': '1.0'})
        self.release_sbom('v1.1.0', {'foreign-package': '9.9'}, repo='other/app')
        out = self.backfill('--release-model', 'two-branch', cliff_out='')
        self.assertNotIn('System packages', out)
        self.assertNotIn('foreign-package', out)

    def test_two_branch_refuses_an_sbom_signed_at_another_commit(self):
        self.build(bump_dep=True)
        self.release_sbom('v1.0.0', {'busybox': '1.0'})
        self.release_sbom(
            'v1.1.0', {'foreign-package': '9.9'}, signed_at=self.git('rev-parse', 'v1.0.0')
        )
        out = self.backfill('--release-model', 'two-branch', cliff_out='')
        self.assertNotIn('System packages', out)
        self.assertNotIn('foreign-package', out)

    def test_two_branch_accepts_an_sbom_a_later_repair_signed(self):
        # The repair runs in the next main run, at the commit that also takes the next tag.
        self.build(bump_dep=True, third=True)
        self.release_sbom('v1.0.0', {'busybox': '1.0'})
        self.release_sbom('v1.1.0', {'busybox': '1.1'}, signed_at=self.git('rev-parse', 'v1.2.0'))
        self.release_sbom('v1.2.0', {'busybox': '1.2'})
        out = self.backfill(
            '--release-model', 'two-branch', cliff_out='', FAKE_TAGS='v1.0.0 v1.1.0 v1.2.0'
        )
        self.assertIn('+- `busybox` 1.0 to 1.1', out)
        self.assertIn('+- `busybox` 1.1 to 1.2', out)

    def test_two_branch_refuses_an_sbom_signed_past_the_next_release(self):
        self.build(bump_dep=True, third=True)
        self.commit({'main.go': 'package main\n\n// fix 3\n'}, 'fix: third fix (#7)')
        self.release_sbom('v1.0.0', {'busybox': '1.0'})
        self.release_sbom('v1.1.0', {'busybox': '1.1'}, signed_at=self.git('rev-parse', 'HEAD'))
        self.release_sbom('v1.2.0', {'busybox': '1.2'})
        out = self.backfill(
            '--release-model', 'two-branch', cliff_out='', FAKE_TAGS='v1.0.0 v1.1.0 v1.2.0'
        )
        self.assertNotIn('1.0 to 1.1', out)
        self.assertNotIn('1.1 to 1.2', out)
        verified = (self.tmp / 'cosign.argv').read_text(encoding='utf-8')
        self.assertIn(
            f'--certificate-github-workflow-sha {self.git("rev-parse", "v1.2.0")} ', verified
        )
        self.assertNotIn(
            f'--certificate-github-workflow-sha {self.git("rev-parse", "HEAD")} ', verified
        )

    def test_two_branch_middle_release_sbom_serves_both_windows(self):
        self.build(bump_dep=True, third=True)
        self.release_sbom('v1.0.0', {'busybox': '1.0'})
        self.release_sbom('v1.1.0', {'busybox': '1.1'})
        self.release_sbom('v1.2.0', {'busybox': '1.2'})
        out = self.backfill(
            '--release-model',
            'two-branch',
            cliff_out='### Fixed\n\n- Root fix (#2)\n',
            FAKE_TAGS='v1.0.0 v1.1.0 v1.2.0',
        )
        self.assertIn('+- `busybox` 1.0 to 1.1', out)
        self.assertIn('+- `busybox` 1.1 to 1.2', out)
        verified = (self.tmp / 'cosign.argv').read_text(encoding='utf-8')
        self.assertEqual(verified.count('verify-blob --bundle '), 3)

    def test_two_branch_never_publishes_a_tampered_sbom(self):
        self.build(bump_dep=True)
        self.release_sbom('v1.0.0', {'busybox': '1.0'})
        self.release_sbom('v1.1.0', {'busybox': '1.1'}, shipped={'busybox': '6.6.6-forged'})
        out = self.backfill('--release-model', 'two-branch', '--apply', cliff_out='')
        self.assertIn('applied v1.1.0', out)
        edited = (self.tmp / 'v1.1.0.edited.md').read_text(encoding='utf-8')
        self.assertIn('**Full changelog**', edited)
        self.assertNotIn('System packages', edited)
        self.assertNotIn('forged', edited)

    def test_two_branch_needs_a_bundle_beside_the_sbom(self):
        self.build(bump_dep=True)
        self.release_sbom('v1.0.0', {'busybox': '1.0'})
        self.release_sbom('v1.1.0', {'busybox': '1.1'})
        (self.tmp / 'v1.1.0.spdx.json.sigstore.json').unlink()
        out = self.backfill('--release-model', 'two-branch', cliff_out='')
        self.assertNotIn('System packages', out)

    def test_two_branch_without_sboms_omits_the_package_part(self):
        self.build(bump_dep=True)
        out = self.backfill('--release-model', 'two-branch', cliff_out='')
        self.assertIn('+**Full changelog**', out)
        self.assertNotIn('System packages', out)

    def pulls(self, base: str, *prs: tuple[str, str, list[str]]) -> None:
        """Merged pull requests into `base`: (head ref, merge commit, labels)."""
        body = [
            {
                'merged_at': '2099-01-01T00:00:00Z',
                'updated_at': '2099-01-01T00:00:00Z',
                'head': {'ref': ref},
                'labels': [{'name': n} for n in labels],
                'merge_commit_sha': sha,
            }
            for ref, sha, labels in prs
        ]
        (self.tmp / f'pulls-{base}.json').write_text(json.dumps(body), encoding='utf-8')

    def test_two_branch_marks_only_updates_merged_through_a_security_pr(self):
        self.build(bump_dep=True, bump_both=True)
        dep, other = self.git('rev-parse', 'v1.1.0~1'), self.git('rev-parse', 'v1.1.0')
        self.pulls('dev', ('renovate/dev-example.com-dep', dep, ['security']))
        self.pulls(
            'main',
            ('renovate/main-weekly-dependencies', other, ['dependencies']),
            ('fix/not-renovate', other, ['security']),
        )
        out = self.backfill('--release-model', 'two-branch', cliff_out='')
        self.assertIn('+- `example.com/dep` v1.0.0 to v1.1.0 (Go, security update)', out)
        self.assertIn('+- `example.com/other` v1.0.0 to v1.2.0 (Go)', out)
        reads = (self.tmp / 'gh-api.argv').read_text(encoding='utf-8')
        for base in ('main', 'dev'):
            self.assertIn(f'repos/owner/app/pulls?state=closed&base={base}&sort=updated', reads)

    def test_legacy_reads_no_pull_requests(self):
        self.build(bump_dep=True)
        self.backfill(cliff_out='### Fixed\n\n- Root fix (#2)\n')
        self.assertFalse((self.tmp / 'gh-api.argv').exists())

    def test_two_branch_compare_link_stands_in_for_an_empty_change_list(self):
        self.build(bump_dep=False)
        out = self.backfill('--release-model', 'two-branch', cliff_out='\n')
        self.assertIn(
            '+**Full changelog**: https://github.com/owner/app/compare/v1.0.0...v1.1.0', out
        )
        self.assertNotIn('maintenance stub', out)
        self.assertNotIn('Commits in this range', out)

    def test_two_branch_with_no_release_pair_is_a_clean_no_op(self):
        self.build(bump_dep=False)
        for tags in ('', 'v1.0.0', 'v1.0.0 v1.1.0-dev.1'):
            for only in ([], ['--only', 'v1.0.0']):
                proc = self.backfill_proc(
                    '--release-model', 'two-branch', *only, cliff_out='', FAKE_TAGS=tags
                )
                self.assertEqual(proc.returncode, 0, (tags, only, proc.stderr))
                self.assertIn('nothing to do: fewer than two semver releases', proc.stdout)
                self.assertNotIn('Traceback', proc.stderr)
        self.assertFalse((self.tmp / 'gh-api.argv').exists())
        self.assertFalse((self.tmp / 'cliff.argv').exists())

    def test_two_branch_refuses_an_only_tag_with_no_predecessor_before_any_read(self):
        self.build(bump_dep=False)
        for tag in ('v9.9.9', 'v1.0.0'):
            proc = self.backfill_proc('--release-model', 'two-branch', '--only', tag, cliff_out='')
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn(
                f'error: --only tag(s) not in the backfillable set: {tag} (backfillable: v1.1.0)',
                proc.stderr,
            )
            self.assertNotIn('Traceback', proc.stderr)
        self.assertFalse((self.tmp / 'gh-api.argv').exists())
        self.assertFalse((self.tmp / 'cliff.argv').exists())

    def rest_calls(self) -> list[list[str]]:
        argv = (self.tmp / 'gh.argv').read_text(encoding='utf-8').splitlines()
        return [json.loads(line) for line in argv]

    def test_apply_patches_the_release_by_id_over_rest_and_reads_it_back(self):
        self.build(bump_dep=True)
        out = self.backfill('--apply', cliff_out='### Fixed\n\n- Root fix (#2)\n')
        self.assertIn('applied v1.1.0', out)
        edited = (self.tmp / 'v1.1.0.edited.md').read_text(encoding='utf-8')
        self.assertEqual(edited, '### Fixed\n\n- Root fix (#2)')
        calls = self.rest_calls()
        self.assertEqual(calls[0], ['api', '-i', 'repos/owner/app'])
        self.assertEqual(calls[1], ['api', '-i', 'repos/owner/app/releases?per_page=100&page=1'])
        patches = [c for c in calls if '-X' in c]
        self.assertEqual(
            patches, [['api', '-i', '-X', 'PATCH', 'repos/owner/app/releases/1001', '--input', '-']]
        )
        reads = [c for c in calls if c[-1] == 'repos/owner/app/releases/tags/v1.1.0']
        self.assertEqual(len(reads), 4, 'the plan, the re-check, the edit and the read-back')
        self.assertFalse([c for c in calls if c[:1] != ['api']], 'no GraphQL-backed gh command')

    def test_an_origin_that_is_not_github_is_refused_before_any_read(self):
        self.build(bump_dep=False)
        self.git('remote', 'set-url', 'origin', 'https://example.com/owner/app.git')
        proc = self.backfill_proc(cliff_out='')
        self.assertEqual(proc.returncode, 2)
        self.assertIn(
            'origin is not a github.com remote: https://example.com/owner/app.git', proc.stderr
        )
        self.assertFalse((self.tmp / 'gh.argv').exists())

    def test_a_listing_longer_than_the_cap_is_refused(self):
        self.build(bump_dep=False)
        proc = self.backfill_proc(cliff_out='', FAKE_FULL_PAGES='1')
        self.assertEqual(proc.returncode, 2)
        self.assertIn('Refusing to proceed on a possibly-truncated release list.', proc.stderr)
        pages = [c for c in self.rest_calls() if '/releases?' in c[-1]]
        self.assertEqual(len(pages), 10)

    def test_a_draft_release_is_skipped(self):
        self.build(bump_dep=False)
        proc = self.backfill_proc(cliff_out='### Fixed\n\n- x\n', FAKE_DRAFTS='v1.2.0')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('skip v1.2.0: draft release', proc.stderr)
        self.assertIn('== v1.1.0: v1.0.0..v1.1.0', proc.stdout)

    def test_the_remote_url_forms_github_names(self):
        for url in (
            'https://github.com/owner/app.git',
            'https://github.com/owner/app',
            'git@github.com:owner/app.git',
            'ssh://git@github.com/owner/app.git',
        ):
            with self.subTest(url=url):
                self.assertEqual(backfill.GITHUB_REMOTE.fullmatch(url)[1], 'owner/app')
        for url in ('https://github.com/owner', '/srv/git/app.git', 'https://github.com.evil/o/a'):
            with self.subTest(url=url):
                self.assertIsNone(backfill.GITHUB_REMOTE.fullmatch(url))


def load_backfill():
    spec = importlib.util.spec_from_file_location('backfill', BACKFILL)
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves the module's annotations through sys.modules.
    sys.modules['backfill'] = module
    spec.loader.exec_module(module)
    return module


backfill = load_backfill()

LIST = """\
carve_outs:
  - repo: owner/one
    tag: v2.0.0
    reason: hand-written migration steps
  - repo: owner/other
    tag: v1.1.0
    reason: provenance note
"""
PAIRS = [('v1.0.0', 'v1.1.0'), ('v1.1.0', 'v2.0.0'), ('v2.0.0', 'v2.1.0')]


class CarveOutListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'carve-outs.yaml'

    def load(self, text: str, repo: str) -> dict[str, str]:
        self.path.write_text(text, encoding='utf-8')
        return backfill.load_carve_outs(self.path, repo)

    def assert_refused(self, text: str) -> str:
        self.path.write_text(text, encoding='utf-8')
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            backfill.load_carve_outs(self.path, 'owner/one')
        self.assertEqual(raised.exception.code, 2)
        self.assertIn(str(self.path), err.getvalue())
        return err.getvalue()

    def test_only_this_repos_entries_are_returned(self):
        self.assertEqual(self.load(LIST, 'owner/one'), {'v2.0.0': 'hand-written migration steps'})
        self.assertEqual(self.load(LIST, 'owner/none'), {})

    def test_a_malformed_list_exits_2_and_names_the_file(self):
        self.assert_refused('carve_outs: {}\n')
        self.assert_refused('carve_outs: [\n')
        self.assertIn('entry 2', self.assert_refused(LIST.replace('tag: v1.1.0', 'tag: 1.1.0')))
        self.assertIn(
            'entry 1',
            self.assert_refused(LIST.replace('    reason: hand-written migration steps\n', '')),
        )

    def test_a_missing_list_exits_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            backfill.load_carve_outs(self.path, 'owner/one')
        self.assertEqual(raised.exception.code, 2)

    def test_the_committed_list_loads(self):
        carved = backfill.load_carve_outs(backfill.CARVE_OUTS, 'cplieger/web-terminal-engine')
        self.assertEqual(sorted(carved), ['v2.7.0', 'v5.0.0'])
        self.assertTrue(all(carved.values()))


class SelectPairsTests(unittest.TestCase):
    def test_a_carved_tag_is_skipped_with_its_reason(self):
        selected, skipped = backfill.select_pairs(
            PAIRS, [], {'v2.0.0': 'why'}, include_carved=False
        )
        self.assertEqual(selected, [('v1.0.0', 'v1.1.0'), ('v2.0.0', 'v2.1.0')])
        self.assertEqual(skipped, [('v2.0.0', 'why')])

    def test_include_carved_plans_every_pair(self):
        selected, skipped = backfill.select_pairs(PAIRS, [], {'v2.0.0': 'why'}, include_carved=True)
        self.assertEqual(selected, PAIRS)
        self.assertEqual(skipped, [])

    def test_only_still_skips_a_carved_tag(self):
        selected, skipped = backfill.select_pairs(
            PAIRS, ['v2.0.0', 'v2.1.0'], {'v2.0.0': 'why'}, include_carved=False
        )
        self.assertEqual(selected, [('v2.0.0', 'v2.1.0')])
        self.assertEqual(skipped, [('v2.0.0', 'why')])

    def test_nothing_carved_selects_the_only_set(self):
        selected, skipped = backfill.select_pairs(PAIRS, ['v1.1.0'], {}, include_carved=False)
        self.assertEqual(selected, [('v1.0.0', 'v1.1.0')])
        self.assertEqual(skipped, [])


if __name__ == '__main__':
    unittest.main()
