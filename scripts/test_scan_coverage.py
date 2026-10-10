"""scan_coverage.py over Trivy and govulncheck reports trimmed from real runs of the
pinned scanners (testdata/scan-coverage); the busybox entries in the arm64 report
and the stdlib finding in the govulncheck stream are added in the same shape. The
cosign line is docker-nut-upsd's real SBOM attestation with its SPDX packages trimmed
to five, so its signature no longer verifies; cosign verified the original."""

from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from types import SimpleNamespace
from typing import ClassVar

SCRIPTS = pathlib.Path(__file__).resolve().parent
TESTDATA = SCRIPTS / 'testdata' / 'scan-coverage'
sys.path.insert(0, str(SCRIPTS))
import promote  # noqa: E402
import scan_coverage as sc  # noqa: E402

GIT_ENV = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
PLATFORMS = [
    {'platform': 'linux/amd64', 'slug': 'linux-amd64', 'ref': 'ghcr.io/cplieger/demo@sha256:a'},
    {'platform': 'linux/arm64', 'slug': 'linux-arm64', 'ref': 'ghcr.io/cplieger/demo@sha256:b'},
]
FRAGMENT_DOCKERFILE = """FROM alpine AS b
RUN cat > /out/demo.cdx.json <<EOF
{"bomFormat": "CycloneDX", "components": [{"type": "application", "name": "Newlib",
  "version": "1.0", "purl": "pkg:generic/newlib@1.0"}]}
EOF
"""


def fixture(name: str):
    return json.loads((TESTDATA / name).read_text())


ATTESTATION = (TESTDATA / 'cosign-spdx-attestation.jsonl').read_text()
STATEMENT = json.loads(base64.b64decode(json.loads(ATTESTATION)['payload']))
NUT_INDEX = 'sha256:' + STATEMENT['subject'][0]['digest']['sha256']


def spdx(*fragments: tuple[str, str]) -> dict:
    """An SPDX document whose fragment packages are (name, image path) pairs."""
    pkgs = [
        {
            'name': 'busybox',
            'versionInfo': '1.37.0',
            'sourceInfo': 'acquired package info from APK DB: /lib/apk/db/installed',
        }
    ]
    pkgs += [
        {'name': n, 'versionInfo': '1.0', 'sourceInfo': f'acquired package info from SBOM: {path}'}
        for n, path in fragments
    ]
    return {'spdxVersion': 'SPDX-2.3', 'packages': pkgs}


def ids(findings):
    return sorted((f['id'], f['package'], f['class']) for f in findings)


class TrivyReducer(unittest.TestCase):
    def test_a_filesystem_report_splits_go_mod_from_the_lockfile(self):
        got = sc.trivy_findings(fixture('trivy-fs.json'), platform=None)
        by_class = {f['package']: f['class'] for f in got}
        self.assertEqual(by_class, {'golang.org/x/text': 'manifest', 'lodash': 'lockfile'})
        lodash = next(f for f in got if f['id'] == 'CVE-2021-23337')
        self.assertEqual(
            lodash,
            {
                'id': 'CVE-2021-23337',
                'package': 'lodash',
                'installed': '4.17.20',
                'fixed': '4.17.21',
                'severity': 'HIGH',
                'class': 'lockfile',
                'targets': ['package-lock.json'],
                'platforms': [],
                'sources': ['trivy-fs'],
            },
        )

    def test_image_os_packages_are_os_and_a_go_binary_splits_stdlib(self):
        os_pkgs = sc.trivy_findings(fixture('trivy-image-linux-amd64.json'), platform='linux/amd64')
        self.assertEqual({f['class'] for f in os_pkgs}, {'os'})
        self.assertEqual({tuple(f['targets']) for f in os_pkgs}, {('image',)})
        gobin = sc.trivy_findings(fixture('trivy-image-gobinary.json'), platform='linux/amd64')
        self.assertEqual(
            {f['package']: f['class'] for f in gobin},
            {'stdlib': 'stdlib', 'golang.org/x/crypto': 'manifest'},
        )
        self.assertEqual({tuple(f['targets']) for f in gobin}, {('age-decrypt',)})

    def test_below_high_is_dropped_and_no_fix_is_null(self):
        got = sc.trivy_findings(fixture('trivy-image-linux-arm64.json'), platform='linux/arm64')
        self.assertNotIn('CVE-2026-99002', {f['id'] for f in got})
        unfixed = next(f for f in got if f['id'] == 'CVE-2026-99001')
        self.assertIsNone(unfixed['fixed'])

    def test_anything_but_a_trivy_report_is_refused(self):
        for bad in (
            [],
            {'Results': []},
            {'SchemaVersion': 2, 'Results': [{'Vulnerabilities': [{}]}]},
        ):
            with self.subTest(report=bad), self.assertRaises(ValueError):
                sc.trivy_findings(bad, platform=None)

    def test_two_platforms_with_one_finding_merge_into_one_record(self):
        merged = sc.merge(
            sc.trivy_findings(fixture('trivy-image-linux-amd64.json'), platform='linux/amd64')
            + sc.trivy_findings(fixture('trivy-image-linux-arm64.json'), platform='linux/arm64')
        )
        shared = [f for f in merged if f['id'] == 'CVE-2026-14456' and f['package'] == 'libssl3']
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]['platforms'], ['linux/amd64', 'linux/arm64'])
        only_arm = next(f for f in merged if f['id'] == 'CVE-2026-99001')
        self.assertEqual(only_arm['platforms'], ['linux/arm64'])
        # Fixable records sort first.
        self.assertIsNone(merged[-1]['fixed'])
        self.assertTrue(all(f['fixed'] for f in merged[:-1]))

    def test_a_fix_on_one_platform_is_not_attributed_to_another(self):
        def record(platform, fixed):
            return {
                'id': 'CVE-1',
                'package': 'libssl3',
                'installed': '3.0.1',
                'fixed': fixed,
                'severity': 'HIGH',
                'class': 'os',
                'targets': ['image'],
                'platforms': [platform],
                'sources': ['trivy-image'],
            }

        for name, arm_fix in (('fixed and unfixed', None), ('two fixes', '3.0.3')):
            with self.subTest(name):
                merged = sc.merge([record('linux/amd64', '3.0.2'), record('linux/arm64', arm_fix)])
                self.assertEqual(
                    sorted((f['platforms'], f['fixed'] or '') for f in merged),
                    [(['linux/amd64'], '3.0.2'), (['linux/arm64'], arm_fix or '')],
                )


class GovulncheckReducer(unittest.TestCase):
    def test_only_called_vulnerabilities_are_findings(self):
        got = sc.govulncheck_findings((TESTDATA / 'govulncheck-root.json').read_text(), '.')
        self.assertEqual(
            ids(got),
            [
                ('GO-2021-0113', 'golang.org/x/text', 'manifest'),
                ('GO-2026-9001', 'stdlib', 'stdlib'),
            ],
        )
        text = next(f for f in got if f['id'] == 'GO-2021-0113')
        self.assertEqual(
            (text['installed'], text['fixed'], text['targets']), ('v0.3.5', 'v0.3.7', ['go.mod'])
        )
        self.assertIsNone(text['severity'])

    def test_a_nested_module_names_its_own_go_mod(self):
        got = sc.govulncheck_findings((TESTDATA / 'govulncheck-root.json').read_text(), 'lane/x')
        self.assertEqual({tuple(f['targets']) for f in got}, {('lane/x/go.mod',)})

    def test_a_stream_without_its_config_record_or_cut_short_is_refused(self):
        whole = (TESTDATA / 'govulncheck-root.json').read_text()
        for bad in ('', '{"finding": {}}', whole[: len(whole) // 2], '[1, 2]'):
            with self.subTest(text=bad[:30]), self.assertRaises(ValueError):
                sc.govulncheck_findings(bad, '.')


WIDGET = json.dumps({'bomFormat': 'CycloneDX', 'components': [{'name': 'Widget'}]})
EMITTER_DOCKERFILE = """FROM alpine AS b
RUN ./scripts/emit-sbom.sh > /out/widget.cdx.json
FROM scratch
COPY --from=b /out/widget.cdx.json /usr/share/sbom/widget.cdx.json
"""


class NotCovered(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def components(self, repo, dockerfile, files=None):
        for name, body in (files or {}).items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(body)
        return sc.source_components(repo, dockerfile, self.root)

    def test_the_from_source_components_are_listed_without_any_finding(self):
        self.assertEqual(self.components('subflux', ''), (['ffmpeg', 'libx264'], []))
        self.assertEqual(self.components('docker-age', ''), ([], []))
        self.assertEqual(
            self.components('docker-nut-upsd', ''), (['libmodbus', 'net-snmp', 'nut'], [])
        )

    def test_a_repo_outside_the_map_with_no_fragment_has_none(self):
        self.assertEqual(self.components('docker-age', 'FROM scratch\n'), ([], []))

    def test_a_fragment_the_map_does_not_know_is_listed(self):
        self.assertEqual(self.components('docker-age', FRAGMENT_DOCKERFILE), (['newlib'], []))
        self.assertEqual(
            self.components('subflux', FRAGMENT_DOCKERFILE), (['ffmpeg', 'libx264', 'newlib'], [])
        )

    def test_a_staged_fragment_resolves_through_the_copy_that_ships_it(self):
        dockerfile = FRAGMENT_DOCKERFILE + (
            'FROM scratch\n'
            'COPY --from=b /out/demo.cdx.json /usr/share/sbom/demo.cdx.json\n'
            'RUN test -s /usr/share/sbom/demo.cdx.json\n'
        )
        self.assertEqual(self.components('docker-age', dockerfile), (['newlib'], []))

    def test_a_printf_fragment_shipped_by_a_directory_copy_resolves(self):
        dockerfile = (
            'FROM alpine AS b\n'
            'RUN mkdir -p /out/usr/share/sbom \\\n'
            '    && printf \'{"bomFormat":"CycloneDX","components":[{"name":"postfix"}]}\' \\\n'
            '        >/out/usr/share/sbom/postfix.cdx.json\n'
            'FROM scratch\n'
            'COPY --from=b /out/usr/share/sbom/ /usr/share/sbom/\n'
            'RUN sbom=/usr/share/sbom/postfix.cdx.json && test -s "$sbom"\n'
        )
        self.assertEqual(self.components('docker-age', dockerfile), (['postfix'], []))

    def test_a_fragment_copied_from_the_checkout_is_read_from_the_checkout(self):
        dockerfile = 'FROM scratch\nCOPY sbom/widget.cdx.json /usr/share/sbom/widget.cdx.json\n'
        self.assertEqual(
            self.components('docker-age', dockerfile, {'sbom/widget.cdx.json': WIDGET}),
            (['widget'], []),
        )

    def test_a_directory_copied_from_the_checkout_carries_its_fragments(self):
        nested = json.dumps(
            {
                'bomFormat': 'CycloneDX',
                'metadata': {'component': {'name': 'Kit'}},
                'components': [{'name': 'Outer', 'components': [{'name': 'Inner'}]}],
            }
        )
        files = {'sbom/a/widget.cdx.json': WIDGET, 'sbom/kit.cdx.json': nested, 'sbom/x.txt': ''}
        got = self.components('docker-age', 'FROM scratch\nCOPY sbom/ /usr/share/sbom/\n', files)
        self.assertEqual(got, (['inner', 'kit', 'outer', 'widget'], []))

    def test_a_copied_fragment_that_names_nothing_is_a_problem(self):
        dockerfile = 'FROM scratch\nCOPY sbom/widget.cdx.json /usr/share/sbom/\n'
        cases = {
            'the fragment sbom/widget.cdx.json is not in the checkout': None,
            'the fragment sbom/widget.cdx.json names no component': '{"components": []}',
            'the fragment sbom/widget.cdx.json does not read as JSON': '{"comp',
        }
        for want, body in cases.items():
            with self.subTest(want=want):
                self.tearDown()
                self.setUp()
                files = {} if body is None else {'sbom/widget.cdx.json': body}
                names, problems = self.components('docker-age', dockerfile, files)
                self.assertEqual(names, [])
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith(want), problems)

    def test_a_fragment_outside_the_checkout_or_fetched_is_a_problem(self):
        for src, want in (
            ('../widget.cdx.json', 'the fragment ../widget.cdx.json is outside the checkout'),
            (
                'https://example.com/w.cdx.json',
                'the fragment https://example.com/w.cdx.json is fetched at build time',
            ),
        ):
            with self.subTest(src=src):
                got = self.components('docker-age', f'FROM scratch\nADD {src} /usr/share/sbom/\n')
                self.assertEqual(got, ([], [want]))

    def test_a_fragment_a_script_emits_is_a_problem_unless_its_path_is_declared(self):
        self.assertEqual(
            self.components('docker-age', EMITTER_DOCKERFILE),
            ([], ['no component name resolves for /usr/share/sbom/widget.cdx.json']),
        )
        declared = {'docker-age': {'/usr/share/sbom/widget.cdx.json': ('widget',)}}
        with unittest.mock.patch.dict(sc.FROM_SOURCE, declared):
            self.assertEqual(self.components('docker-age', EMITTER_DOCKERFILE), (['widget'], []))
        other = {'docker-age': {'/usr/share/sbom/other.cdx.json': ('widget',)}}
        with unittest.mock.patch.dict(sc.FROM_SOURCE, other):
            self.assertEqual(
                self.components('docker-age', EMITTER_DOCKERFILE),
                (['widget'], ['no component name resolves for /usr/share/sbom/widget.cdx.json']),
            )

    def test_a_declared_repo_that_gains_an_unnamed_fragment_has_a_problem(self):
        dockerfile = (
            'FROM alpine AS build\n'
            'RUN ./scripts/emit-sbom.sh > /out/dav1d.cdx.json\n'
            'FROM alpine\n'
            'COPY --from=build /out/dav1d.cdx.json /usr/share/sbom/dav1d.cdx.json\n'
        )
        self.assertEqual(
            self.components('subflux', dockerfile),
            (
                ['ffmpeg', 'libx264'],
                ['no component name resolves for /usr/share/sbom/dav1d.cdx.json'],
            ),
        )

    def test_an_opaque_fragment_is_not_named_by_another_stage_writing_the_same_path(self):
        dockerfile = (
            'FROM alpine AS decoy\n'
            'RUN cat > /out/widget.cdx.json <<EOF\n'
            '{"bomFormat": "CycloneDX", "components": [{"name": "decoy"}]}\n'
            'EOF\n'
            'FROM alpine AS real\n'
            'RUN ./scripts/emit-sbom.sh > /out/widget.cdx.json\n'
            'FROM scratch\n'
            'COPY --from=real /out/widget.cdx.json /usr/share/sbom/widget.cdx.json\n'
        )
        self.assertEqual(
            self.components('docker-age', dockerfile),
            ([], ['no component name resolves for /usr/share/sbom/widget.cdx.json']),
        )

    def test_a_named_fragment_the_final_image_never_copies_is_not_listed(self):
        dockerfile = FRAGMENT_DOCKERFILE + 'FROM scratch\nCOPY --from=b /out/app /app\n'
        self.assertEqual(self.components('docker-age', dockerfile), ([], []))
        by_dir = FRAGMENT_DOCKERFILE + 'FROM scratch\nCOPY --from=b /out/ /srv/\n'
        self.assertEqual(self.components('docker-age', by_dir), (['newlib'], []))

    def test_a_stage_built_from_another_inherits_its_fragments(self):
        dockerfile = FRAGMENT_DOCKERFILE + (
            'FROM b AS test\n'
            'RUN test -s /out/demo.cdx.json\n'
            'FROM alpine AS base\n'
            'COPY --from=test /out/demo.cdx.json /usr/share/sbom/demo.cdx.json\n'
            'FROM base AS final\n'
            'COPY --from=test /tests-passed /tests-passed\n'
        )
        self.assertEqual(self.components('docker-age', dockerfile), (['newlib'], []))

    def test_a_fragment_copied_from_an_external_image_is_unnamed(self):
        dockerfile = 'FROM scratch\nCOPY --from=ghcr.io/x/y:1 /sbom/y.cdx.json /usr/share/sbom/\n'
        self.assertEqual(
            self.components('docker-age', dockerfile),
            ([], ['no component name resolves for /usr/share/sbom/y.cdx.json']),
        )

    def test_a_pathless_fragment_in_a_stage_the_image_may_not_ship_is_a_problem(self):
        writes = 'RUN printf \'{"bomFormat":"CycloneDX","components":[{"name":"x"}]}\' > "$O"\n'
        self.assertEqual(self.components('docker-age', 'FROM alpine\n' + writes), (['x'], []))
        self.assertEqual(
            self.components('docker-age', 'FROM alpine AS b\n' + writes + 'FROM scratch\n'),
            ([], ['a stage the image may not ship writes a CycloneDX document to no named path']),
        )

    def test_a_cyclonedx_document_with_no_name_is_a_problem(self):
        dockerfile = 'FROM alpine\nRUN echo \'{"bomFormat": "CycloneDX"}\' > "$OUT"\n'
        self.assertEqual(
            self.components('docker-age', dockerfile),
            ([], ['an instruction writes a CycloneDX document naming no component']),
        )

    def test_a_comment_naming_a_fragment_is_not_one(self):
        dockerfile = 'FROM scratch\n# Syft reads /usr/share/sbom/x.cdx.json files.\nRUN \\\n  # x.cdx.json\\\n  true\n'
        self.assertEqual(self.components('docker-age', dockerfile), ([], []))

    def test_instructions_are_read_in_any_case(self):
        upper = (
            'FROM alpine AS b\nRUN ./emit.sh > /out/w.cdx.json\nFROM scratch\n'
            'COPY --from=b /out/w.cdx.json /usr/share/sbom/w.cdx.json\n'
        )
        want = ([], ['no component name resolves for /usr/share/sbom/w.cdx.json'])
        for dockerfile in (upper, upper.lower(), upper.replace('COPY', 'Copy')):
            with self.subTest(dockerfile=dockerfile):
                self.assertEqual(self.components('docker-new', dockerfile), want)

    def test_a_continuation_or_heredoc_line_never_starts_an_instruction(self):
        dockerfile = (
            'from alpine as b\n'
            'healthcheck --interval=30s \\\n'
            '\n'
            '  cmd ["/app", "health"]\n'
            'run apk add tini \\\n'
            '  # a comment keeps the instruction open\n'
            'env CGO_ENABLED=0 true\n'
            'run <<-EOF\n'
            '\tcopy is a word here\n'
            '\tEOF\n'
            'from scratch\n'
        )
        self.assertEqual(
            [kw for kw, _ in sc.instructions(dockerfile)],
            ['FROM', 'HEALTHCHECK', 'RUN', 'RUN', 'FROM'],
        )

    def test_a_left_shift_is_no_heredoc_and_hides_no_unnamed_fragment(self):
        dockerfile = (
            'FROM alpine AS build\n'
            'RUN ./emit > /out/real.cdx.json && echo $((1<<N))\n'
            'FROM scratch\n'
            'RUN echo "name":"decoy" > /usr/share/sbom/decoy.cdx.json\n'
            'COPY --from=build /out/real.cdx.json /usr/share/sbom/real.cdx.json\n'
        )
        self.assertEqual(
            [kw for kw, _ in sc.instructions(dockerfile)], ['FROM', 'RUN', 'FROM', 'RUN', 'COPY']
        )
        self.assertEqual(
            self.components('docker-new', dockerfile),
            (['decoy'], ['no component name resolves for /usr/share/sbom/real.cdx.json']),
        )

    def test_a_heredoc_opens_only_on_a_whole_shell_word(self):
        cases = {
            'shift operand and quoted markers': (
                'RUN echo $((x<<n)) "<<EOF" \'<<EOF\' a<<EOF\nfrom x\nEOF\n',
                ['FROM', 'RUN', 'FROM'],
            ),
            'quoted terminator': (
                'RUN cat <<"EOF" > /f\nfrom x\nEOF\nfrom y\n',
                ['FROM', 'RUN', 'FROM'],
            ),
            'fd and tab-strip': (
                "RUN cat 3<<-'EOF'\nfrom x\n\tEOF\nfrom y\n",
                ['FROM', 'RUN', 'FROM'],
            ),
            'two heredocs': (
                'RUN <<A cat - <<B\nfrom x\nA\nfrom y\nB\nfrom z\n',
                ['FROM', 'RUN', 'FROM'],
            ),
            'onbuild': ('ONBUILD RUN <<EOF\nfrom x\nEOF\n', ['FROM', 'ONBUILD']),
            'not a heredoc instruction': (
                'LABEL a <<EOF\nfrom x\nEOF\n',
                ['FROM', 'LABEL', 'FROM'],
            ),
            'body after the continuation': (
                'RUN cat <<EOF \\\n  && echo hi\nfrom x\nEOF\nfrom y\n',
                ['FROM', 'RUN', 'FROM'],
            ),
        }
        for name, (body, want) in cases.items():
            with self.subTest(name):
                got = [kw for kw, _ in sc.instructions('FROM scratch\n' + body)]
                self.assertEqual(got, want)

    def test_only_a_covered_component_leaves_the_list(self):
        with unittest.mock.patch.object(sc, 'COVERED', frozenset({'nut'})):
            self.assertEqual(
                self.components('docker-nut-upsd', ''), (['libmodbus', 'net-snmp'], [])
            )


class Platforms(unittest.TestCase):
    def run_cmd(self):
        out = []
        with unittest.mock.patch('builtins.print', side_effect=out.append):
            rc = sc.cmd_platforms(SimpleNamespace(repo='demo'))
        return rc, out

    def test_each_platform_is_a_matrix_row_with_a_digest_ref(self):
        with (
            unittest.mock.patch.object(promote, 'registry_token', return_value='t'),
            unittest.mock.patch.object(promote, 'tag_digest', return_value='sha256:i') as tag,
            unittest.mock.patch.object(
                promote,
                'platforms',
                return_value={'linux/arm64': 'sha256:b', 'linux/amd64': 'sha256:a'},
            ),
        ):
            rc, out = self.run_cmd()
        self.assertEqual(rc, 0)
        tag.assert_called_once_with('demo', 'latest', 't')
        self.assertEqual(out[0], 'index=sha256:i')
        self.assertEqual(json.loads(out[1].removeprefix('platforms=')), PLATFORMS)

    def test_an_unreadable_image_is_an_error_line_and_no_rows(self):
        with unittest.mock.patch.object(
            promote,
            'registry_token',
            side_effect=promote.GhError('GET x: HTTP Error 404\nNot Found'),
        ):
            rc, out = self.run_cmd()
        self.assertEqual(rc, 0)
        self.assertEqual(
            out,
            ['platforms=[]', 'error=ghcr.io/cplieger/demo:latest: GET x: HTTP Error 404 Not Found'],
        )


def git_tree(root: pathlib.Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(body)
    subprocess.run(['git', 'init', '-q', str(root)], check=True, env=GIT_ENV)
    subprocess.run(['git', '-C', str(root), 'add', '-A'], check=True, env=GIT_ENV)


class GoModules(unittest.TestCase):
    def test_modules_with_source_outside_vendor_and_testdata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            git_tree(
                root,
                {
                    'go.mod': 'module x\n',
                    'main.go': 'package main\n',
                    'lane/sub/go.mod': 'module x/lane/sub\n',
                    'lane/sub/a.go': 'package sub\n',
                    'web/go.mod': 'module web-ignore\n',
                    'web/app.ts': '',
                    'vendor/y/go.mod': 'module y\n',
                    'vendor/y/y.go': 'package y\n',
                    'internal/testdata/m/go.mod': 'module m\n',
                    'internal/testdata/m/m.go': 'package m\n',
                },
            )
            (root / 'untracked').mkdir()
            (root / 'untracked' / 'go.mod').write_text('module u\n')
            (root / 'untracked' / 'u.go').write_text('package u\n')
            self.assertEqual(sc.go_modules(root), [('.', 'root'), ('lane/sub', 'lane__sub')])

    def test_a_file_belongs_to_its_nearest_module_only(self):
        cases = {
            'source only in a nested module': (
                {'go.mod': 'module x\n', 'lane/go.mod': 'module x/lane\n', 'lane/x.go': ''},
                [('lane', 'lane')],
            ),
            'source only in a nested module under testdata': (
                {'go.mod': 'module x\n', 'testdata/m/go.mod': 'module m\n', 'testdata/m/m.go': ''},
                [],
            ),
            'root source only under testdata': (
                {'go.mod': 'module x\n', 'internal/testdata/t.go': ''},
                [],
            ),
            'a sibling directory sharing a prefix': (
                {
                    'go.mod': 'module x\n',
                    'lane/go.mod': 'module x/lane\n',
                    'lane/x.go': '',
                    'lanes/y.go': '',
                },
                [('.', 'root'), ('lane', 'lane')],
            ),
        }
        for name, (files, want) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                git_tree(pathlib.Path(tmp), files)
                self.assertEqual(sc.go_modules(pathlib.Path(tmp)), want)


class SignedSbom(unittest.TestCase):
    ARGV: ClassVar[list[str]] = [
        'cosign',
        'verify-attestation',
        '--type',
        'spdxjson',
        '--certificate-oidc-issuer',
        'https://token.actions.githubusercontent.com',
        '--certificate-identity-regexp',
        r'^https://github\.com/cplieger/ci/\.github/workflows/docker-release\.yaml@',
        f'ghcr.io/cplieger/docker-nut-upsd@{NUT_INDEX}',
    ]

    def runner(self, *answers):
        calls, sleeps = [], []

        def run(argv, **_):
            calls.append(argv)
            rc, out, err = answers[min(len(calls), len(answers)) - 1]
            return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

        return run, calls, sleeps

    def test_the_real_attestation_lists_the_components_the_map_declares(self):
        run, calls, sleeps = self.runner((0, ATTESTATION, ''))
        docs = sc.signed_sboms('docker-nut-upsd', NUT_INDEX, run=run, sleep=sleeps.append)
        self.assertEqual(calls, [self.ARGV])
        self.assertEqual(
            sc.sbom_fragments(docs),
            {path: set(names) for path, names in sc.FROM_SOURCE['docker-nut-upsd'].items()},
        )

    def test_a_failed_verification_is_retried_twice_then_refused(self):
        run, calls, sleeps = self.runner((1, '', 'x\nError: no matching attestations'))
        with self.assertRaisesRegex(ValueError, 'no matching attestations$'):
            sc.signed_sboms('docker-nut-upsd', NUT_INDEX, run=run, sleep=sleeps.append)
        self.assertEqual((len(calls), sleeps), (3, [10, 20]))
        run, calls, sleeps = self.runner((1, '', 'flake'), (0, ATTESTATION, ''))
        self.assertEqual(
            len(sc.signed_sboms('docker-nut-upsd', NUT_INDEX, run=run, sleep=sleeps.append)), 1
        )
        self.assertEqual((len(calls), sleeps), (2, [10]))

    def test_an_attestation_of_another_digest_or_garbage_is_refused(self):
        run, _, sleeps = self.runner((0, ATTESTATION, ''))
        with self.assertRaisesRegex(ValueError, 'no SPDX attestation of'):
            sc.signed_sboms('docker-nut-upsd', 'sha256:' + '0' * 64, run=run, sleep=sleeps.append)
        run, _, _ = self.runner((0, '{"payload": "%%%"}\n', ''))
        with self.assertRaisesRegex(ValueError, 'unreadable attestation'):
            sc.signed_sboms('docker-nut-upsd', NUT_INDEX, run=run, sleep=sleeps.append)

    def test_the_subcommand_records_a_failure_instead_of_an_empty_sbom(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / 'sbom.json'
            boom = ValueError('cosign verify-attestation x: no signatures found')
            with (
                unittest.mock.patch.object(sc, 'signed_sboms', side_effect=boom),
                unittest.mock.patch('builtins.print') as printed,
            ):
                rc = sc.main(['sbom', 'docker-nut-upsd', 'sha256:i', '--out', str(out)])
            self.assertEqual(rc, 0)
            self.assertEqual(
                json.loads(out.read_text()),
                {'index': 'sha256:i', 'error': 'cosign verify-attestation x: no signatures found'},
            )
            printed.assert_called_once_with(
                '::warning::signed SBOM: cosign verify-attestation x: no signatures found'
            )


class Summarize(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.root, self.reports = base / 'checkout', base / 'reports'
        self.root.mkdir()
        self.reports.mkdir()
        git_tree(
            self.root,
            {'go.mod': 'module x\n', 'main.go': 'package main\n', 'Dockerfile': 'FROM scratch\n'},
        )
        for name in (
            'trivy-fs.json',
            'trivy-image-linux-amd64.json',
            'trivy-image-linux-arm64.json',
        ):
            shutil.copy(TESTDATA / name, self.reports / name)
        shutil.copy(TESTDATA / 'govulncheck-root.json', self.reports / 'govulncheck-root.json')
        (self.reports / 'govulncheck-modules.tsv').write_text('.\troot\tok\n')
        self.write_sbom = True

    def tearDown(self):
        self.tmp.cleanup()

    def sbom(self, record):
        (self.reports / 'sbom.json').write_text(json.dumps(record))

    def summarize(self, repo='docker-nut-upsd', **over):
        if self.write_sbom and not (self.reports / 'sbom.json').exists():
            doc = STATEMENT['predicate'] if repo == 'docker-nut-upsd' else spdx()
            self.sbom({'index': over.get('--index', 'sha256:i'), 'documents': [doc]})
        argv = {
            '--repo': repo,
            '--commit': '0123456789abcdef0123',
            '--reports': str(self.reports),
            '--image': 'true',
            '--platforms': json.dumps(PLATFORMS),
            '--index': 'sha256:i',
            '--image-error': '',
            '--image-job': 'success',
            '--out': str(self.root / 'security-main.json'),
            '--summary': str(self.root / 'summary.md'),
            **over,
        }
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / 'scan_coverage.py'), 'summarize']
            + [x for kv in argv.items() for x in kv],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
            env=GIT_ENV,
        )
        doc = json.loads((self.root / 'security-main.json').read_text())
        return proc, doc, (self.root / 'summary.md').read_text()

    def test_a_complete_scan_records_every_source_and_the_not_covered_components(self):
        proc, doc, summary = self.summarize()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(doc['complete'])
        self.assertEqual(doc['errors'], [])
        self.assertEqual(doc['schema'], 1)
        self.assertEqual(doc['repo'], 'cplieger/docker-nut-upsd')
        self.assertEqual(
            doc['image'],
            {
                'ref': 'ghcr.io/cplieger/docker-nut-upsd:latest',
                'digest': 'sha256:i',
                'platforms': {'linux/amd64': 'sha256:a', 'linux/arm64': 'sha256:b'},
            },
        )
        self.assertEqual(
            {s for f in doc['findings'] for s in f['sources']},
            {'trivy-fs', 'trivy-image', 'govulncheck'},
        )
        self.assertEqual(doc['not_covered'], ['libmodbus', 'net-snmp', 'nut'])
        self.assertIn('### Not covered by the scan', summary)
        self.assertIn('- net-snmp', summary)
        self.assertIn('| CVE-2021-23337 | lodash | 4.17.20 | 4.17.21 | HIGH | lockfile |', summary)
        self.assertIn('1 finding(s) have no fix yet', summary)

    def test_a_clean_scan_still_lists_the_not_covered_components(self):
        empty = {'SchemaVersion': 2, 'ArtifactName': 'x', 'ArtifactType': 'filesystem'}
        for name in (
            'trivy-fs.json',
            'trivy-image-linux-amd64.json',
            'trivy-image-linux-arm64.json',
        ):
            (self.reports / name).write_text(json.dumps(empty))
        (self.reports / 'govulncheck-root.json').write_text('{"config": {}}')
        proc, doc, summary = self.summarize(repo='subflux')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(doc['findings'], [])
        self.assertEqual(doc['not_covered'], ['ffmpeg', 'libx264'])
        self.assertIn('No fixable finding.', summary)
        self.assertIn('- ffmpeg', summary)

    def test_a_fragment_copied_from_the_checkout_is_listed_and_the_scan_complete(self):
        (self.root / 'Dockerfile').write_text(
            'FROM scratch\nCOPY sbom/widget.cdx.json /usr/share/sbom/widget.cdx.json\n'
        )
        (self.root / 'sbom').mkdir()
        (self.root / 'sbom' / 'widget.cdx.json').write_text(WIDGET)
        proc, doc, summary = self.summarize(repo='docker-age')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(doc['complete'])
        self.assertEqual(doc['not_covered'], ['widget'])
        self.assertIn('- widget', summary)

    def test_a_fragment_no_name_resolves_for_makes_the_scan_incomplete(self):
        (self.root / 'Dockerfile').write_text(EMITTER_DOCKERFILE)
        proc, doc, summary = self.summarize(repo='docker-age')
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(doc['complete'])
        self.assertEqual(doc['not_covered'], [])
        want = 'source components: no component name resolves for /usr/share/sbom/widget.cdx.json'
        self.assertEqual(doc['errors'], [want])
        self.assertIn(f'::error::{want}', proc.stdout)
        self.assertIn('The scan is incomplete', summary)

    def test_each_missing_or_broken_report_makes_the_scan_incomplete(self):
        def drop(name):
            def run():
                (self.reports / name).unlink()

            return run

        def garble(name):
            return lambda: (self.reports / name).write_text('{"SchemaVersion": 2, "Res')

        cases = {
            'Trivy filesystem scan: no report': drop('trivy-fs.json'),
            'Trivy filesystem scan: unreadable report': garble('trivy-fs.json'),
            'Trivy image scan of linux/arm64: no report': drop('trivy-image-linux-arm64.json'),
            'Trivy image scan of linux/amd64: unreadable': garble('trivy-image-linux-amd64.json'),
            'govulncheck: no module list': drop('govulncheck-modules.tsv'),
            'govulncheck of .: no report': drop('govulncheck-root.json'),
            'govulncheck of .: not a govulncheck JSON stream': garble('govulncheck-root.json'),
        }
        for want, breakit in cases.items():
            with self.subTest(want=want):
                self.tearDown()
                self.setUp()
                breakit()
                proc, doc, summary = self.summarize()
                self.assertEqual(proc.returncode, 1)
                self.assertFalse(doc['complete'])
                self.assertTrue(any(e.startswith(want) for e in doc['errors']), doc['errors'])
                self.assertIn('The scan is incomplete', summary)
                self.assertNotIn('No fixable finding.', summary)
                self.assertIn(f'::error::{want}', proc.stdout)

    def test_an_incomplete_scan_with_nothing_found_never_reads_as_clean(self):
        empty = {'SchemaVersion': 2, 'ArtifactName': 'x', 'ArtifactType': 'container_image'}
        for name in ('trivy-image-linux-amd64.json', 'trivy-image-linux-arm64.json'):
            (self.reports / name).write_text(json.dumps(empty))
        (self.reports / 'govulncheck-root.json').write_text('{"config": {}}')
        (self.reports / 'trivy-fs.json').unlink()
        proc, doc, summary = self.summarize()
        self.assertEqual((proc.returncode, doc['findings']), (1, []))
        self.assertIn('The scan is incomplete', summary)
        self.assertNotIn('No fixable finding.', summary)

    def test_a_failed_module_names_govulncheck_s_last_error_line(self):
        (self.reports / 'govulncheck-modules.tsv').write_text('.\troot\terror\n')
        (self.reports / 'govulncheck-root.err').write_text('loading\ngo: download failed\n')
        proc, doc, _ = self.summarize()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(doc['errors'], ['govulncheck of .: failed: go: download failed'])

    def test_a_module_the_list_omits_is_not_run(self):
        (self.reports / 'govulncheck-modules.tsv').write_text('other\tother\tok\n')
        _, doc, _ = self.summarize()
        self.assertEqual(doc['errors'], ['govulncheck of .: not run'])

    def test_an_unpublished_or_unresolved_image_is_an_error_never_no_image(self):
        _, doc, _ = self.summarize(**{'--platforms': '[]', '--image-error': 'GET x: 404'})
        self.assertIsNone(doc['image'])
        self.assertEqual(doc['errors'], ['published image: GET x: 404'])
        _, doc, _ = self.summarize(**{'--platforms': '', '--image-job': 'failure'})
        self.assertEqual(doc['errors'], ['published image: not resolved (job failure)'])

    def test_an_opaque_script_s_fragment_is_listed_from_the_signed_sbom(self):
        (self.root / 'Dockerfile').write_text('FROM alpine\nRUN ./scripts/build-and-sbom.sh\n')
        self.sbom(
            {'index': 'sha256:i', 'documents': [spdx(('Hidden', '/usr/share/sbom/x.cdx.json'))]}
        )
        proc, doc, summary = self.summarize(repo='docker-age')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(doc['complete'])
        self.assertEqual(doc['not_covered'], ['hidden'])
        self.assertIn('- hidden', summary)

    def test_the_signed_sbom_names_a_fragment_the_dockerfile_does_not(self):
        (self.root / 'Dockerfile').write_text(EMITTER_DOCKERFILE)
        self.sbom(
            {
                'index': 'sha256:i',
                'documents': [spdx(('Widget', '/usr/share/sbom/widget.cdx.json'))],
            }
        )
        proc, doc, _ = self.summarize(repo='docker-age')
        self.assertEqual((proc.returncode, doc['errors']), (0, []))
        self.assertEqual(doc['not_covered'], ['widget'])

    def test_a_missing_failed_or_foreign_sbom_makes_the_scan_incomplete(self):
        cases = [
            ('signed SBOM: no report (sbom.json)', None),
            (
                'signed SBOM: cosign verify-attestation x: no signatures found',
                {'index': 'sha256:i', 'error': 'cosign verify-attestation x: no signatures found'},
            ),
            ('signed SBOM: not the SBOM of sha256:i', {'index': 'sha256:other', 'documents': []}),
            ('signed SBOM: not an SPDX document', {'index': 'sha256:i', 'documents': [{'x': 1}]}),
            (
                'signed SBOM: not an SPDX document',
                {'index': 'sha256:i', 'documents': [{'packages': []}]},
            ),
        ]
        for want, record in cases:
            with self.subTest(want=want):
                self.write_sbom = record is not None
                (self.reports / 'sbom.json').unlink(missing_ok=True)
                if record is not None:
                    self.sbom(record)
                proc, doc, summary = self.summarize()
                self.assertEqual(proc.returncode, 1)
                self.assertFalse(doc['complete'])
                self.assertEqual(doc['errors'], [want])
                self.assertEqual(doc['not_covered'], ['libmodbus', 'net-snmp', 'nut'])
                self.assertIn('The scan is incomplete', summary)

    def test_a_repo_with_no_image_expects_no_image_report(self):
        for name in ('trivy-image-linux-amd64.json', 'trivy-image-linux-arm64.json'):
            (self.reports / name).unlink()
        self.write_sbom = False
        proc, doc, _ = self.summarize(**{'--image': 'false', '--platforms': '', '--index': ''})
        self.assertEqual(proc.returncode, 0, doc['errors'])
        self.assertIsNone(doc['image'])

    def test_a_repo_with_no_go_module_expects_no_govulncheck_report(self):
        subprocess.run(['git', '-C', str(self.root), 'rm', '-q', '--cached', 'go.mod'], check=True)
        for name in ('govulncheck-modules.tsv', 'govulncheck-root.json'):
            (self.reports / name).unlink()
        proc, doc, _ = self.summarize()
        self.assertEqual(proc.returncode, 0, doc['errors'])
        self.assertNotIn('govulncheck', {s for f in doc['findings'] for s in f['sources']})


if __name__ == '__main__':
    unittest.main()
