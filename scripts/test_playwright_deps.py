"""scripts/playwright-deps.sh, the browser system-library install, and the steps
that run it: the apt drop-in, the bounded retry, the .deb cache and the step
timeouts that hold every attempt."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts' / 'playwright-deps.sh'
WORKFLOWS = ROOT / '.github' / 'workflows'
CI_TOOLS = '${{ runner.temp }}/cplieger-ci/scripts'
CACHE = '${{ runner.temp }}/playwright-debs'
PREFIX = 'playwright-debs-ubuntu-26.04-webkit-0123-'
# (workflow, job, install step)
SITES = (
    ('ts-ci.yaml', 'validate', 'Install browsers (vitest browser mode)'),
    ('ts-ci.yaml', 'engines-floor', 'Install browsers (vitest browser mode)'),
    ('perf-measure.yaml', 'measure', 'Install browsers'),
)

SUDO_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG/sudo"
exec "$@"
"""
# Each call takes the next word of $STUB_NPX: `stall` sleeps past the attempt
# bound, so the real `timeout` ends it with 124; a number is the exit status.
# It logs the archive's .deb files as the install found them.
NPX_STUB = """#!/usr/bin/env bash
n=$(cat "$STUB_LOG/calls" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$STUB_LOG/calls"
printf '%s | %s\\n' "$*" "$(cd "$ARCHIVES" && ls)" >> "$STUB_LOG/npx"
read -ra plan <<< "$STUB_NPX"
case "${plan[$((n - 1))]:-0}" in
  stall) exec sleep 60 ;;
  0) echo kept > "$ARCHIVES/libnew_1.0_amd64.deb" ;;
  *) echo 'E: Failed to fetch http://archive.ubuntu.com/x.deb  Connection timed out'; exit "${plan[$((n - 1))]}" ;;
esac
"""
LOG_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG/$(basename "$0")"
"""
# The hosted image's own drop-in, from actions/runner-images configure-apt.sh.
IMAGE_RETRIES = (
    'Acquire::Retries "1";\nAcquire::http::Timeout "15";\nAcquire::https::Timeout "15";\n'
)
OURS = {
    'Acquire::http::Timeout': '20',
    'Acquire::https::Timeout': '20',
    'Acquire::Retries': '5',
    'APT::Keep-Downloaded-Packages': 'true',
    'DPkg::Lock::Timeout': '60',
}


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def steps(workflow: str, job: str) -> list[dict]:
    return load(workflow)['jobs'][job]['steps']


def named(workflow: str, job: str, name: str) -> dict:
    return next(s for s in steps(workflow, job) if s.get('name') == name)


def setting(name: str) -> int:
    return int(
        re.search(rf'^{name}=(?:\$\{{\w+:-)?(\d+)\}}?$', SCRIPT.read_text(), re.MULTILINE)[1]
    )


def apt_config(apt_conf: Path, key: str) -> str:
    return subprocess.run(
        ['apt-config', 'dump', '--no-empty', '--format', '%v', key],
        env={**os.environ, 'APT_CONFIG': str(apt_conf)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout


BROWSER_LISTS = ('firefox', 'webkit', 'chromium,firefox', 'chromium,firefox,webkit')


def render_key(text: str, browsers: str) -> str:
    """The key text with each expression replaced by its value on a hosted runner
    running `browsers`; an expression it does not know fails the test."""

    def value(m: re.Match) -> str:
        expr = m[1].strip()
        if expr == "inputs.runner || 'ubuntu-26.04'":
            return 'ubuntu-26.04'
        if engine := re.fullmatch(r"contains\(env\.CPLIEGER_BROWSERS, '(\w+)'\)", expr):
            return str(engine[1] in browsers.split(',')).lower()
        if expr.startswith('hashFiles('):
            return 'f' * 64
        raise AssertionError(f'unknown expression in a cache key: {expr}')

    return re.sub(r'\$\{\{(.*?)\}\}', value, text)


def names_sum(*packages: str) -> str:
    names = ''.join(f'{p}_1.0_amd64.deb\n' for p in sorted(packages))
    return hashlib.sha256(names.encode()).hexdigest()


class Run:
    def __init__(self, proc: subprocess.CompletedProcess, tmp: Path):
        self.rc, self.out, self.tmp = proc.returncode, proc.stdout + proc.stderr, tmp

    def logged(self, name: str) -> list[str]:
        path = self.tmp / 'log' / name
        return path.read_text().splitlines() if path.exists() else []

    def outputs(self) -> str:
        path = self.tmp / 'output'
        return path.read_text() if path.exists() else ''


@unittest.skipIf(shutil.which('apt-config') is None, 'the script checks apt with apt-config')
class Script(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='playwright-deps-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for d in ('bin', 'log', 'archives', 'apt.conf.d'):
            (self.tmp / d).mkdir()
        self.parts = self.tmp / 'apt.conf.d'
        self.conf = self.parts / 'zzzz-cplieger-ci-retries'
        (self.parts / 'zz-retries').write_text(IMAGE_RETRIES)
        # apt-config reads this file, then the parts directory it names, then the main file.
        self.apt_conf = self.tmp / 'apt-config'
        self.apt_conf.write_text(
            f'Dir::Etc::parts "{self.parts}";\nDir::Etc::main "{self.tmp / "apt.conf"}";\n'
        )
        for name, text in (
            ('sudo', SUDO_STUB),
            ('npx', NPX_STUB),
            ('dpkg', LOG_STUB),
            ('apt-get', LOG_STUB),
        ):
            (self.tmp / 'bin' / name).write_text(text)
            (self.tmp / 'bin' / name).chmod(0o755)

    def run_script(self, *engines: str, npx: str = '0', **env: str) -> Run:
        full = {
            'PATH': os.pathsep.join([str(self.tmp / 'bin'), '/usr/bin', '/bin']),
            'HOME': str(self.tmp),
            'STUB_LOG': str(self.tmp / 'log'),
            'STUB_NPX': npx,
            'ARCHIVES': str(self.tmp / 'archives'),
            'PLAYWRIGHT_DEPS_ARCHIVES': str(self.tmp / 'archives'),
            'PLAYWRIGHT_DEPS_CONF': str(self.conf),
            'APT_CONFIG': str(self.apt_conf),
            'PLAYWRIGHT_DEPS_ATTEMPT_SECONDS': '1',
            'PLAYWRIGHT_DEPS_SLEEP': '0',
            'GITHUB_OUTPUT': str(self.tmp / 'output'),
            'PLAYWRIGHT_DEPS_CACHE_KEY': PREFIX,
            **env,
        }
        proc = subprocess.run(
            ['bash', str(SCRIPT), *engines],
            env=full,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return Run(proc, self.tmp)

    def calls(self, run: Run) -> list[str]:
        return [line.split(' | ')[0] for line in run.logged('npx')]

    def test_the_drop_in_is_written_before_the_install(self):
        run = self.run_script('webkit')
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(
            self.conf.read_text().splitlines(), [f'{k} "{v}";' for k, v in OURS.items()]
        )
        self.assertEqual(run.logged('sudo')[0], f'tee {self.conf}')
        self.assertEqual(len(run.logged('npx')), 1)

    def test_the_drop_in_sorts_after_the_image_s_and_its_values_win(self):
        default = re.search(
            r'^CONF=\$\{PLAYWRIGHT_DEPS_CONF:-(.+)\}$', SCRIPT.read_text(), re.MULTILINE
        )[1]
        self.assertEqual(os.path.dirname(default), '/etc/apt/apt.conf.d')
        self.assertEqual(os.path.basename(default), self.conf.name)
        # apt sorts the directory's names by byte, as str comparison does for ASCII.
        self.assertGreater(self.conf.name, 'zz-retries')
        run = self.run_script('webkit')
        self.assertEqual(run.rc, 0, run.out)
        for key, value in OURS.items():
            with self.subTest(key=key):
                self.assertEqual(apt_config(self.apt_conf, key), value)
                self.assertIn(f'apt uses {key} "{value}"', run.out)

    def test_a_later_sorting_file_fails_the_step_before_the_install(self):
        (self.parts / 'zzzzz-later').write_text('Acquire::Retries "1";\n')
        run = self.run_script('webkit')
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn(
            f'::error::apt uses Acquire::Retries "1", not "5" from {self.conf}: ', run.out
        )
        self.assertNotIn('::error::apt uses Acquire::http::Timeout', run.out)
        self.assertEqual(run.logged('npx'), [])

    def test_apt_conf_overriding_a_value_fails_the_step(self):
        (self.tmp / 'apt.conf').write_text('dpkg::lock::timeout "0";\n')
        run = self.run_script('webkit')
        self.assertEqual(run.rc, 1, run.out)
        self.assertIn('::error::apt uses DPkg::Lock::Timeout "0", not "60"', run.out)
        self.assertEqual(run.logged('npx'), [])

    def test_a_first_success_runs_once_and_repairs_nothing(self):
        run = self.run_script('firefox', 'webkit')
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(self.calls(run), ['--no-install playwright install-deps firefox webkit'])
        self.assertEqual(run.logged('dpkg'), [])
        self.assertNotIn('::warning::', run.out)

    def test_an_attempt_past_its_bound_is_retried_after_a_dpkg_repair(self):
        run = self.run_script('webkit', npx='stall 0')
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(len(self.calls(run)), 2)
        self.assertIn('attempt 1/3 failed (exit 124)', run.out)
        self.assertEqual(run.logged('dpkg'), ['--configure -a'])

    def test_retries_stop_at_the_first_success(self):
        run = self.run_script('webkit', npx='1 stall 0')
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(len(self.calls(run)), 3)
        self.assertIn('attempt 1/3 failed (exit 1)', run.out)
        self.assertIn('attempt 2/3 failed (exit 124)', run.out)

    def test_spent_attempts_fail_loudly_with_apt_s_error(self):
        run = self.run_script('webkit', npx='1 stall 100 0')
        self.assertEqual(run.rc, 1, run.out)
        self.assertEqual(len(self.calls(run)), 3)
        self.assertIn('Connection timed out', run.out)
        self.assertIn(
            '::error::playwright install-deps webkit failed 3 times, the last with exit 100 ',
            run.out,
        )
        self.assertIn('Ubuntu mirror or DNS outage', run.out)

    def test_a_self_hosted_runner_installs_nothing_and_needs_no_root(self):
        run = self.run_script('webkit', RUNNER_ENVIRONMENT='self-hosted')
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.logged('sudo'), [])
        self.assertEqual(run.logged('npx'), [])
        self.assertIn('skipping playwright install-deps webkit', run.out)

    def test_no_engine_is_refused(self):
        run = self.run_script()
        self.assertEqual(run.rc, 2, run.out)
        self.assertEqual(run.logged('npx'), [])

    def test_the_cache_seeds_apt_and_is_refreshed_after_a_success(self):
        cache = self.tmp / 'cache'
        cache.mkdir()
        (cache / 'libold_1.0_amd64.deb').write_text('old')
        (cache / 'notes.txt').write_text('not a deb')
        run = self.run_script('webkit', PLAYWRIGHT_DEPS_CACHE=str(cache))
        self.assertEqual(run.rc, 0, run.out)
        # The install saw the cached deb in apt's archive.
        self.assertEqual(
            run.logged('npx'),
            ['--no-install playwright install-deps webkit | libold_1.0_amd64.deb'],
        )
        self.assertEqual(run.logged('apt-get'), ['autoclean -qq'])
        self.assertEqual(
            sorted(p.name for p in cache.glob('*.deb')),
            ['libnew_1.0_amd64.deb', 'libold_1.0_amd64.deb'],
        )
        self.assertEqual(run.outputs(), f'key={PREFIX}{names_sum("libnew", "libold")}\n')

    def test_the_saved_key_follows_the_deb_set(self):
        keys = []
        for cached in (('libold',), ('libold',), ('libold', 'libupdate')):
            cache = Path(tempfile.mkdtemp(dir=self.tmp))
            for name in cached:
                (cache / f'{name}_1.0_amd64.deb').write_text(name)
            (self.tmp / 'output').unlink(missing_ok=True)
            (self.tmp / 'log' / 'calls').unlink(missing_ok=True)
            for deb in (self.tmp / 'archives').glob('*.deb'):
                deb.unlink()
            run = self.run_script('webkit', PLAYWRIGHT_DEPS_CACHE=str(cache))
            self.assertEqual(run.rc, 0, run.out)
            keys.append(run.outputs())
        self.assertEqual(keys[0], keys[1])
        self.assertNotEqual(keys[1], keys[2])
        self.assertEqual(keys[2], f'key={PREFIX}{names_sum("libnew", "libold", "libupdate")}\n')

    def test_a_cache_without_a_key_prefix_saves_nothing(self):
        cache = self.tmp / 'cache'
        run = self.run_script(
            'webkit', PLAYWRIGHT_DEPS_CACHE=str(cache), PLAYWRIGHT_DEPS_CACHE_KEY=''
        )
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual([p.name for p in cache.glob('*.deb')], ['libnew_1.0_amd64.deb'])
        self.assertEqual(run.outputs(), '')

    def test_the_refresh_drops_what_autoclean_removed(self):
        cache = self.tmp / 'cache'
        cache.mkdir()
        (cache / 'libgone_1.0_amd64.deb').write_text('gone')
        (self.tmp / 'bin' / 'apt-get').write_text(
            '#!/usr/bin/env bash\nrm -f "$ARCHIVES/libgone_1.0_amd64.deb"\n'
        )
        run = self.run_script('webkit', PLAYWRIGHT_DEPS_CACHE=str(cache))
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual([p.name for p in cache.glob('*.deb')], ['libnew_1.0_amd64.deb'])

    def test_no_kept_deb_saves_no_cache(self):
        (self.tmp / 'bin' / 'apt-get').write_text('#!/usr/bin/env bash\nrm -f "$ARCHIVES"/*.deb\n')
        run = self.run_script('webkit', PLAYWRIGHT_DEPS_CACHE=str(self.tmp / 'cache'))
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.outputs(), '')
        self.assertIn('kept no .deb files', run.out)

    def test_a_failed_install_saves_no_cache(self):
        run = self.run_script('webkit', npx='1 1 1', PLAYWRIGHT_DEPS_CACHE=str(self.tmp / 'c'))
        self.assertEqual(run.rc, 1, run.out)
        self.assertEqual(run.outputs(), '')
        self.assertEqual(run.logged('apt-get'), [])

    def test_without_a_cache_nothing_is_copied_or_cleaned(self):
        run = self.run_script('webkit')
        self.assertEqual(run.rc, 0, run.out)
        self.assertEqual(run.logged('apt-get'), [])
        self.assertEqual(run.outputs(), '')


class Wiring(unittest.TestCase):
    def test_every_library_install_goes_through_the_script(self):
        for path in sorted(WORKFLOWS.glob('*.y*ml')):
            for line in path.read_text().splitlines():
                if 'install-deps' in line or '--with-deps' in line:
                    with self.subTest(workflow=path.name, line=line.strip()):
                        self.assertIn('"$CI_TOOLS/playwright-deps.sh"', line)
        for workflow, job, name in SITES:
            with self.subTest(workflow=workflow, job=job):
                self.assertIn(
                    'bash "$CI_TOOLS/playwright-deps.sh" ', named(workflow, job, name)['run']
                )

    def test_the_step_timeout_holds_every_attempt_and_its_backoff(self):
        attempts, bound = setting('ATTEMPTS'), setting('ATTEMPT_SECONDS')
        kill, delay = setting('KILL_SECONDS'), setting('FIRST_DELAY')
        text = SCRIPT.read_text()
        self.assertIn('timeout -k "$KILL_SECONDS" "$ATTEMPT_SECONDS" ', text)
        self.assertIn('delay=$FIRST_DELAY\n', text)
        self.assertIn('-ge "$ATTEMPTS" ]', text)
        worst = attempts * (bound + kill) + sum(delay * 2**i for i in range(attempts - 1))
        for workflow, job, name in SITES:
            with self.subTest(workflow=workflow, job=job):
                minutes = named(workflow, job, name)['timeout-minutes']
                # The engines' own download follows the libraries within the same step.
                self.assertGreaterEqual(minutes * 60 - worst, 180)

    def test_each_site_restores_and_saves_one_cache_around_the_install(self):
        for workflow, job, name in SITES:
            with self.subTest(workflow=workflow, job=job):
                names = [s.get('name') for s in steps(workflow, job)]
                at = names.index(name)
                self.assertEqual(
                    names[at - 1 : at + 2],
                    ['Restore the browser libraries', name, 'Save the browser libraries'],
                )
                restore = named(workflow, job, 'Restore the browser libraries')
                install = named(workflow, job, name)
                save = named(workflow, job, 'Save the browser libraries')
                self.assertTrue(restore['uses'].startswith('actions/cache/restore@'))
                self.assertTrue(save['uses'].startswith('actions/cache/save@'))
                self.assertEqual(restore['with']['path'], CACHE)
                self.assertEqual(save['with']['path'], CACHE)
                self.assertEqual(install['env']['PLAYWRIGHT_DEPS_CACHE'], CACHE)
                self.assertEqual(install['env']['CI_TOOLS'], CI_TOOLS)
                self.assertEqual(install['id'], 'browsers')
                self.assertEqual(
                    install['env']['PLAYWRIGHT_DEPS_CACHE_KEY'],
                    '${{ steps.debs.outputs.cache-primary-key }}',
                )
                # The entry is saved under its .deb set, and again whenever that set changes.
                self.assertEqual(save['with']['key'], '${{ steps.browsers.outputs.key }}')
                self.assertEqual(
                    save['if'],
                    "${{ steps.browsers.outputs.key != '' && "
                    'steps.browsers.outputs.key != steps.debs.outputs.cache-matched-key }}',
                )
                key = restore['with']['key']
                self.assertIn("inputs.runner || 'ubuntu-26.04'", key)
                self.assertIn('node_modules/playwright-core/package.json', key)
                self.assertTrue(key.endswith(')) }}-'), key)
                self.assertEqual(restore['with']['restore-keys'], key)
                self.assertIn("runner.environment == 'github-hosted'", restore['if'])

    def test_every_cache_key_passes_actions_cache_s_key_check(self):
        for workflow, job, _ in SITES:
            restore = named(workflow, job, 'Restore the browser libraries')
            for field in ('key', 'restore-keys'):
                text = restore['with'][field]
                with self.subTest(workflow=workflow, job=job, field=field):
                    self.assertNotIn('env.CPLIEGER_BROWSERS }}', text)
                    self.assertNotIn('needs.package.outputs.browsers', text)
                    for browsers in BROWSER_LISTS:
                        key = render_key(text, browsers)
                        # The saved key is this prefix plus the script's sha256.
                        for k in (key, key + '0' * 64):
                            self.assertRegex(k, r'^[^,]*$', browsers)
                            self.assertLessEqual(len(k), 512, browsers)

    def test_lists_that_pick_the_same_libraries_share_a_cache(self):
        for workflow, job in (('ts-ci.yaml', 'validate'), ('ts-ci.yaml', 'engines-floor')):
            with self.subTest(job=job):
                key = named(workflow, job, 'Restore the browser libraries')['with']['key']
                self.assertEqual(render_key(key, 'chromium,firefox'), render_key(key, 'firefox'))
                self.assertNotEqual(render_key(key, 'firefox'), render_key(key, 'firefox,webkit'))

    def test_the_ci_source_leaves_the_workspace_before_any_package_step(self):
        for workflow, job, _ in SITES:
            with self.subTest(workflow=workflow, job=job):
                names = [s.get('name') for s in steps(workflow, job)]
                at = names.index('Check out the ci source')
                self.assertEqual(names[at + 1], 'Move the ci source out of the workspace')
                move = named(workflow, job, 'Move the ci source out of the workspace')
                self.assertEqual(
                    move['run'].strip(),
                    'mv "$GITHUB_WORKSPACE/.cplieger-ci" "$RUNNER_TEMP/cplieger-ci"',
                )
                self.assertLess(at, names.index('Setup Node.js'))


if __name__ == '__main__':
    unittest.main()
