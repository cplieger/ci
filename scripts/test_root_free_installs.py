"""The shared CI workflows install their tools without root.

A self-hosted runner's job user cannot write /usr/local/bin or use sudo, so
each install writes to $RUNNER_TEMP/bin and puts it on $GITHUB_PATH.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github' / 'workflows'
CI_WORKFLOWS = (
    'ci.yaml',
    'go-ci.yaml',
    'ts-ci.yaml',
    'shell-ci.yaml',
    'deadset-ci.yaml',
    'security-scan.yaml',
)
ACTION = re.compile(r'^(?:\./\.cplieger-ci/actions/|\./actions/|cplieger/ci/actions/)([\w-]+)')
ROOT_ONLY = re.compile(r'\bsudo\b|/usr/local\b')

CURL_STUB = """#!/usr/bin/env bash
out='' url=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift ;;
    https://*) url=$1 ;;
  esac
  shift
done
printf '%s %s\\n' "${out:--}" "$url" >> "$STUB_LOG"
src="$FIXTURES/${url##*/}"
[ -f "$src" ] || exit 22
if [ -n "$out" ]; then cp "$src" "$out"; else cat "$src"; fi
"""

SUDO_STUB = """#!/usr/bin/env bash
echo "sudo $*" >> "$STUB_LOG"
exit 1
"""

NPM_STUB = """#!/usr/bin/env bash
echo "npm $*" >> "$STUB_LOG"
prefix='' pkg=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) prefix=$2; shift ;;
    -*) ;;
    *) pkg=$1 ;;
  esac
  shift
done
[ -n "$prefix" ] || { echo "EACCES: the default global prefix is root-owned" >&2; exit 243; }
mkdir -p "$prefix/bin"
printf '#!/bin/sh\necho %s\n' "$pkg" > "$prefix/bin/${pkg%@*}"
chmod +x "$prefix/bin/${pkg%@*}"
"""

GOLANGCI_INSTALLER = """#!/bin/sh
set -e
[ "$1" = -b ] || exit 2
mkdir -p "$2"
printf '#!/bin/sh\\necho golangci-lint %s\\n' "$3" > "$2/golangci-lint"
chmod +x "$2/golangci-lint"
"""


def jobs(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())['jobs']


def step(workflow: str, job: str, name: str) -> dict:
    for s in jobs(workflow)[job]['steps']:
        if s.get('name') == name:
            return s
    raise AssertionError(f'{workflow} {job} has no step {name!r}')


def fake_tool(name: str) -> bytes:
    return f'#!/bin/sh\necho {name} fake\n'.encode()


def tgz(member: str, name: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        data = fake_tool(name)
        info = tarfile.TarInfo(member)
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def fixture(tool: str, version: str) -> tuple[str, bytes]:
    """The release asset each install step downloads, by its URL basename."""
    match tool:
        case 'actionlint':
            return f'actionlint_{version[1:]}_linux_amd64.tar.gz', tgz('actionlint', tool)
        case 'lychee':
            target = 'lychee-x86_64-unknown-linux-gnu'
            return f'{target}.tar.gz', tgz(f'{target}/lychee', tool)
        case 'shellcheck':
            member = f'shellcheck-{version}/shellcheck'
            return f'shellcheck-{version}.linux.x86_64.tar.gz', tgz(member, tool)
        case 'shfmt':
            return f'shfmt_{version}_linux_amd64', fake_tool(tool)
        case 'golangci-lint':
            return 'install.sh', GOLANGCI_INSTALLER.encode()
        case 'markdownlint-cli2':
            return '', b''
    raise AssertionError(tool)


INSTALLS = (
    ('ci.yaml', 'repo', 'actionlint'),
    ('ci.yaml', 'repo', 'shellcheck'),
    ('ci.yaml', 'markdown', 'lychee'),
    ('ci.yaml', 'scripts', 'shellcheck'),
    ('ci.yaml', 'scripts', 'shfmt'),
    ('shell-ci.yaml', 'validate', 'shellcheck'),
    ('shell-ci.yaml', 'validate', 'shfmt'),
    ('go-ci.yaml', 'validate', 'golangci-lint'),
    ('ci.yaml', 'markdown', 'markdownlint-cli2'),
)
NO_DOWNLOAD = ('golangci-lint', 'markdownlint-cli2')
NPM_GLOBAL = re.compile(r'^.*\bnpm (?:install|i)\b.*(?:\s-g\b|--global\b).*$', re.MULTILINE)
SETUP_NODE = 'actions/setup-node@'


class NoRootInstalls(unittest.TestCase):
    def test_no_shared_ci_job_uses_sudo_or_writes_usr_local(self):
        for workflow in CI_WORKFLOWS:
            for job, spec in jobs(workflow).items():
                for s in spec.get('steps', []):
                    with self.subTest(workflow=workflow, job=job, step=s.get('name')):
                        self.assertIsNone(ROOT_ONLY.search(s.get('run', '')))

    def test_a_global_npm_install_without_setup_node_names_a_job_prefix(self):
        seen = 0
        for workflow in CI_WORKFLOWS:
            for job, spec in jobs(workflow).items():
                node = False
                for s in spec.get('steps', []):
                    node = node or s.get('uses', '').startswith(SETUP_NODE)
                    for line in NPM_GLOBAL.findall(s.get('run', '')):
                        seen += 1
                        if not node:
                            with self.subTest(workflow=workflow, job=job, step=s.get('name')):
                                self.assertIn('--prefix "$RUNNER_TEMP"', line)
        self.assertGreater(seen, 0)

    def test_no_action_the_shared_ci_jobs_run_uses_sudo_or_writes_usr_local(self):
        names = set()
        for workflow in CI_WORKFLOWS:
            for spec in jobs(workflow).values():
                for s in spec.get('steps', []):
                    if m := ACTION.match(s.get('uses', '')):
                        names.add(m.group(1))
        self.assertIn('alert-rules', names)
        for name in sorted(names):
            for path in sorted((ROOT / 'actions' / name).rglob('*')):
                if path.suffix in ('.yml', '.yaml', '.sh', '.py') and 'test' not in path.name:
                    with self.subTest(file=str(path.relative_to(ROOT))):
                        self.assertIsNone(ROOT_ONLY.search(path.read_text()))


class InstallSteps(unittest.TestCase):
    def run_install(self, workflow: str, job: str, tool: str) -> tuple[Path, Path, str]:
        body = step(workflow, job, f'Install {tool}')['run']
        version = re.search(r'^\s*VERSION=(\S+)$', body, re.MULTILINE).group(1)
        tmp = Path(tempfile.mkdtemp(prefix='root-free-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        stubs, fixtures, runner_temp = tmp / 'stubs', tmp / 'fixtures', tmp / 'runner'
        for d in (stubs, fixtures, runner_temp):
            d.mkdir()
        for name, text in (('curl', CURL_STUB), ('sudo', SUDO_STUB), ('npm', NPM_STUB)):
            (stubs / name).write_text(text)
            (stubs / name).chmod(0o755)
        asset, data = fixture(tool, version)
        if asset:
            (fixtures / asset).write_bytes(data)
        script = tmp / 'step.sh'
        script.write_text(body)
        env = {
            'PATH': os.pathsep.join([str(stubs), '/usr/bin', '/bin']),
            'HOME': str(tmp),
            'RUNNER_TEMP': str(runner_temp),
            'GITHUB_PATH': str(tmp / 'github_path'),
            'FIXTURES': str(fixtures),
            'STUB_LOG': str(tmp / 'log'),
        }
        proc = subprocess.run(
            ['bash', '-e', str(script)], cwd=tmp, env=env, capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        log = (tmp / 'log').read_text()
        self.assertNotIn('sudo', log)
        return runner_temp / 'bin', tmp / 'github_path', log

    def test_each_install_lands_in_the_job_temp_bin_on_the_path(self):
        for workflow, job, tool in INSTALLS:
            with self.subTest(workflow=workflow, job=job, tool=tool):
                bindir, github_path, _ = self.run_install(workflow, job, tool)
                self.assertEqual(github_path.read_text().splitlines(), [str(bindir)])
                self.assertEqual(sorted(p.name for p in bindir.iterdir()), [tool])
                later = os.pathsep.join([str(bindir), '/usr/bin', '/bin'])
                found = shutil.which(tool, path=later)
                self.assertEqual(found, str(bindir / tool))
                ran = subprocess.run([found], capture_output=True, text=True)
                self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_each_archive_is_downloaded_to_a_file_with_the_retry_recipe(self):
        for workflow, job, tool in INSTALLS:
            if tool in NO_DOWNLOAD:
                continue
            with self.subTest(workflow=workflow, job=job, tool=tool):
                body = step(workflow, job, f'Install {tool}')['run']
                self.assertIn('--retry 7 --retry-max-time 150 --retry-all-errors', body)
                self.assertNotRegex(body, r'curl[^\n]*\|')
                _, _, log = self.run_install(workflow, job, tool)
                self.assertNotRegex(log, r'^- ', 'a download went to stdout')

    def test_the_pinned_versions_match_across_workflows(self):
        for tool in ('shellcheck', 'shfmt'):
            with self.subTest(tool=tool):
                versions = {
                    re.search(r'VERSION=(\S+)', step(w, j, f'Install {tool}')['run']).group(1)
                    for w, j, t in INSTALLS
                    if t == tool
                }
                self.assertEqual(len(versions), 1, versions)


POOL_COSIGN = ('published-image', 'trivy-published')


class CosignInstall(unittest.TestCase):
    """The pool-capable security jobs install the cosign the hosted trivy job verified."""

    def run_body(self, body: str, env: dict[str, str], fixture: bytes | None) -> tuple:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='cosign-')))
        stubs, fixtures, runner_temp = tmp / 'stubs', tmp / 'fixtures', tmp / 'runner'
        for d in (stubs, fixtures, runner_temp):
            d.mkdir()
        for name, text in (('curl', CURL_STUB), ('sudo', SUDO_STUB)):
            (stubs / name).write_text(text)
            (stubs / name).chmod(0o755)
        if fixture is not None:
            (fixtures / 'cosign-linux-amd64').write_bytes(fixture)
        full = {
            'PATH': os.pathsep.join([str(stubs), '/usr/bin', '/bin']),
            'HOME': str(tmp),
            'RUNNER_TEMP': str(runner_temp),
            'GITHUB_PATH': str(tmp / 'github_path'),
            'GITHUB_OUTPUT': str(tmp / 'github_output'),
            'FIXTURES': str(fixtures),
            'STUB_LOG': str(tmp / 'log'),
            'COSIGN_VERSION': 'v3.1.4',
            **env,
        }
        proc = subprocess.run(
            ['bash', '-e', '-c', body], cwd=tmp, env=full, capture_output=True, text=True
        )
        return proc, runner_temp / 'bin', tmp

    def test_both_jobs_take_the_digest_from_the_hosted_job_and_skip_the_installer(self):
        spec = jobs('security-scan.yaml')
        self.assertEqual(
            spec['trivy']['outputs']['cosign-sha256'], '${{ steps.cosign.outputs.sha256 }}'
        )
        bodies = set()
        for job in POOL_COSIGN:
            with self.subTest(job=job):
                needs = spec[job]['needs']
                self.assertIn('trivy', [needs] if isinstance(needs, str) else needs)
                s = step('security-scan.yaml', job, 'Install cosign')
                self.assertNotIn('uses', s)
                self.assertEqual(
                    s['env']['COSIGN_SHA256'], '${{ needs.trivy.outputs.cosign-sha256 }}'
                )
                self.assertIn('--retry 7 --retry-max-time 150 --retry-all-errors', s['run'])
                bodies.add(s['run'])
        self.assertEqual(len(bodies), 1)

    def test_a_binary_matching_the_digest_lands_in_the_job_temp_bin(self):
        data = fake_tool('cosign')
        body = step('security-scan.yaml', 'published-image', 'Install cosign')['run']
        sha = hashlib.sha256(data).hexdigest()
        proc, bindir, tmp = self.run_body(body, {'COSIGN_SHA256': sha}, data)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((tmp / 'github_path').read_text().splitlines(), [str(bindir)])
        self.assertEqual(sorted(p.name for p in bindir.iterdir()), ['cosign'])
        ran = subprocess.run([str(bindir / 'cosign')], capture_output=True, text=True)
        self.assertEqual(ran.stdout, 'cosign fake\n')
        log = (tmp / 'log').read_text()
        self.assertIn('/releases/download/v3.1.4/cosign-linux-amd64', log)
        self.assertNotRegex(log, r'^- ')

    def test_a_mismatched_or_missing_digest_installs_nothing(self):
        body = step('security-scan.yaml', 'trivy-published', 'Install cosign')['run']
        for sha in ('0' * 64, ''):
            with self.subTest(sha=sha):
                proc, _, tmp = self.run_body(body, {'COSIGN_SHA256': sha}, fake_tool('cosign'))
                self.assertNotEqual(proc.returncode, 0)
                self.assertFalse((tmp / 'github_path').exists())

    def test_the_hosted_job_records_the_digest_of_the_cosign_it_verified(self):
        s = step('security-scan.yaml', 'trivy', "Record the verified cosign's digest")
        self.assertEqual(s['id'], 'cosign')
        names = [x.get('name') for x in jobs('security-scan.yaml')['trivy']['steps']]
        self.assertLess(names.index('Install Cosign'), names.index(s['name']))
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / 'cosign').write_bytes(fake_tool('cosign'))
        (tmp / 'cosign').chmod(0o755)
        out = tmp / 'out'
        env = {'PATH': os.pathsep.join([str(tmp), '/usr/bin', '/bin']), 'GITHUB_OUTPUT': str(out)}
        proc = subprocess.run(
            ['bash', '-e', '-c', s['run']], env=env, capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        want = hashlib.sha256(fake_tool('cosign')).hexdigest()
        self.assertEqual(out.read_text(), f'sha256={want}\n')


if __name__ == '__main__':
    unittest.main()
