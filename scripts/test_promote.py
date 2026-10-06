"""Tests for the pure decision functions of promote.py."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import promote

NOW = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
A, B, C, D = ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40)


class Ancestry(unittest.TestCase):
    def test_target_must_be_on_first_parent_history(self):
        self.assertTrue(promote.on_first_parent([C, B, A], B))
        self.assertFalse(promote.on_first_parent([C, B, A], D))

    def test_main_is_ancestor_when_target_is_ahead_or_identical(self):
        self.assertTrue(promote.main_is_ancestor({'status': 'ahead', 'ahead_by': 3}))
        self.assertTrue(promote.main_is_ancestor({'status': 'identical', 'ahead_by': 0}))
        self.assertFalse(promote.main_is_ancestor({'status': 'behind', 'ahead_by': 0}))
        self.assertFalse(promote.main_is_ancestor({'status': 'diverged', 'ahead_by': 2}))

    def test_nothing_to_promote_when_identical(self):
        self.assertTrue(promote.nothing_to_promote({'status': 'identical', 'ahead_by': 0}))
        self.assertFalse(promote.nothing_to_promote({'status': 'ahead', 'ahead_by': 1}))

    def test_delta_count_must_match_github(self):
        self.assertTrue(promote.delta_count_matches(3, {'status': 'ahead', 'ahead_by': 3}))
        self.assertFalse(promote.delta_count_matches(2, {'status': 'ahead', 'ahead_by': 3}))
        self.assertFalse(promote.delta_count_matches(3, {'status': 'ahead'}))

    def test_delta_commits_come_from_the_clone_newest_first(self):
        import subprocess
        import tempfile
        from pathlib import Path

        env = {
            'GIT_AUTHOR_NAME': 't',
            'GIT_AUTHOR_EMAIL': 't@example.invalid',
            'GIT_COMMITTER_NAME': 't',
            'GIT_COMMITTER_EMAIL': 't@example.invalid',
            'GIT_CONFIG_GLOBAL': '/dev/null',
            'PATH': __import__('os').environ['PATH'],
        }
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp)

            def g(*args):
                return subprocess.run(
                    ['git', '-C', tmp, *args], check=True, capture_output=True, text=True, env=env
                ).stdout.strip()

            g('init', '-q', '-b', 'dev')
            shas = []
            for i in range(4):
                g('commit', '-q', '--allow-empty', '-m', f'c{i}')
                shas.append(g('rev-parse', 'HEAD'))
            g('update-ref', 'refs/remotes/origin/main', shas[0])
            got, dates = promote.delta_commits(clone, shas[3])
            self.assertEqual(got, [shas[3], shas[2], shas[1]])
            self.assertEqual(len(dates), 3)
            self.assertTrue(all(promote.datetime.fromisoformat(d) for d in dates))


class Purity(unittest.TestCase):
    def test_go_mod_single_line_and_block(self):
        text = (
            'module github.com/cplieger/subflux\n\n'
            'require github.com/cplieger/httpx v5.0.4-dev.1\n'
            'require (\n'
            '\tgithub.com/cplieger/envx v2.1.0\n'
            '\tgithub.com/cplieger/health v1.4.0-dev.2 // indirect\n'
            '\tgolang.org/x/text v0.38.0-dev.9\n'
            ')\n'
        )
        got = promote.purity_violations({'go.mod': text})
        self.assertEqual(
            got,
            [
                'go.mod: github.com/cplieger/httpx v5.0.4-dev.1',
                'go.mod: github.com/cplieger/health v1.4.0-dev.2',
            ],
        )

    def test_go_mod_stable_pins_are_pure(self):
        text = 'module x\nrequire (\n\tgithub.com/cplieger/envx v2.1.0\n)\n'
        self.assertEqual(promote.purity_violations({'go.mod': text}), [])

    def test_go_mod_replace_to_a_dev_version_single_line_and_block(self):
        text = (
            'module x\n'
            'require github.com/cplieger/httpx v5.0.4\n'
            'replace github.com/cplieger/httpx => github.com/cplieger/httpx v5.0.5-dev.1\n'
            'replace (\n'
            '\tgithub.com/cplieger/envx v2.1.0 => github.com/cplieger/envx v2.2.0-dev.3\n'
            '\tgithub.com/cplieger/health => ../health\n'
            '\tgolang.org/x/text => golang.org/x/text v0.38.0-dev.9\n'
            ')\n'
        )
        got = promote.purity_violations({'go.mod': text})
        self.assertEqual(
            got,
            [
                'go.mod: replace github.com/cplieger/httpx => github.com/cplieger/httpx v5.0.5-dev.1',
                'go.mod: replace github.com/cplieger/envx => github.com/cplieger/envx v2.2.0-dev.3',
            ],
        )

    def test_go_mod_replace_to_a_stable_version_is_pure(self):
        text = 'module x\nreplace github.com/cplieger/httpx => github.com/cplieger/httpx v5.0.5\n'
        self.assertEqual(promote.purity_violations({'go.mod': text}), [])

    def test_package_json_alias_spec_naming_a_first_party_dev_version(self):
        text = (
            '{"dependencies": {"httpx-alias": "npm:@cplieger/httpx@5.0.5-dev.1",'
            ' "zod-alias": "npm:zod@3.0.0-dev.1"}}'
        )
        got = promote.purity_violations({'package.json': text})
        self.assertEqual(
            got, ['package.json: dependencies httpx-alias npm:@cplieger/httpx@5.0.5-dev.1']
        )

    def test_package_json_shipping_sections_only(self):
        text = (
            '{"dependencies": {"@cplieger/reactive": "1.2.0-dev.3"},'
            ' "peerDependencies": {"@cplieger/actions": "^2.0.0"},'
            ' "optionalDependencies": {"@cplieger/fetch": "3.0.0-dev.1"},'
            ' "devDependencies": {"@cplieger/ui-primitives": "1.0.0-dev.7"}}'
        )
        got = promote.purity_violations({'web/package.json': text})
        self.assertEqual(
            got,
            [
                'web/package.json: dependencies @cplieger/reactive 1.2.0-dev.3',
                'web/package.json: optionalDependencies @cplieger/fetch 3.0.0-dev.1',
            ],
        )

    def test_jsr_json_imports(self):
        text = '{"imports": {"@cplieger/reactive": "jsr:@cplieger/reactive@1.2.0-dev.3", "zod": "npm:zod@3.0.0-dev.1"}}'
        got = promote.purity_violations({'jsr.json': text})
        self.assertEqual(
            got, ['jsr.json: imports @cplieger/reactive jsr:@cplieger/reactive@1.2.0-dev.3']
        )

    def test_dockerfile_from_and_args(self):
        text = (
            'FROM ghcr.io/cplieger/knell:v2.0.17-dev.3 AS base\n'
            'FROM alpine:3.23.1-dev.1\n'
            '# renovate: datasource=npm depName=@cplieger/web-terminal-ui\n'
            'ARG WTUI_VERSION=5.6.1-dev.2\n'
            '# renovate: datasource=npm depName=@xterm/xterm\n'
            'ARG XTERM_VERSION=5.5.0-dev.1\n'
            'ARG ENGINE=github.com/cplieger/web-terminal-engine@v5.1.0-dev.4\n'
            'ARG PKG_REFRESH=static\n'
        )
        got = promote.purity_violations({'Dockerfile': text})
        self.assertEqual(
            got,
            [
                'Dockerfile: FROM ghcr.io/cplieger/knell:v2.0.17-dev.3',
                'Dockerfile: ARG WTUI_VERSION=5.6.1-dev.2',
                'Dockerfile: ARG ENGINE=github.com/cplieger/web-terminal-engine@v5.1.0-dev.4',
            ],
        )

    def test_renovate_hint_applies_to_the_next_arg_only(self):
        text = (
            '# renovate: datasource=npm depName=@cplieger/web-terminal-ui\n'
            'ARG WTUI_VERSION=5.6.1\n'
            'ARG OTHER=1.0.0-dev.1\n'
        )
        self.assertEqual(promote.purity_violations({'Dockerfile': text}), [])

    def test_renovate_hint_is_disowned_by_any_line_in_between(self):
        # Renovate reads the marker on the line right above the ARG, so a
        # marker followed by other instructions annotates nothing.
        text = (
            '# renovate: datasource=docker depName=ghcr.io/cplieger/knell\n'
            'FROM alpine:3.23 AS base\n'
            'RUN apk add --no-cache ca-certificates\n'
            'ARG OTHER=1.0.0-dev.1\n'
        )
        self.assertEqual(promote.purity_violations({'Dockerfile': text}), [])

    def test_purity_file_selection(self):
        self.assertTrue(promote.is_purity_file('go.mod'))
        self.assertTrue(promote.is_purity_file('web/jsr.json'))
        self.assertTrue(promote.is_purity_file('Dockerfile.arm64'))
        self.assertFalse(promote.is_purity_file('static-src/node_modules/x/package.json'))
        self.assertFalse(promote.is_purity_file('main.go'))


class Classification(unittest.TestCase):
    def test_machine_prefixes(self):
        for head in ('renovate/go-deps', 'repo-sync/ci/default', 'rebuild/20260920'):
            self.assertEqual(promote.classify_commit([{'head': {'ref': head}}]), 'machine', head)

    def test_any_other_head_is_human(self):
        self.assertEqual(promote.classify_commit([{'head': {'ref': 'feat/x'}}]), 'human')
        self.assertEqual(promote.classify_commit([{'head': {'ref': 'renovatex/y'}}]), 'human')

    def test_no_pull_request_is_human(self):
        self.assertEqual(promote.classify_commit([]), 'human')

    def test_every_commit_is_classified_however_long_the_delta(self):
        shas = [f'{i:040x}' for i in range(300)]
        human_at = shas[250]

        def pulls_for(sha):
            head = 'feat/x' if sha == human_at else 'renovate/x'
            return [{'head': {'ref': head}}]

        self.assertEqual(promote.first_human_commit(shas, pulls_for), human_at)
        self.assertIsNone(
            promote.first_human_commit(shas, lambda sha: [{'head': {'ref': 'renovate/x'}}])
        )

    def test_classification_stops_at_the_first_human_commit(self):
        shas = [f'{i:040x}' for i in range(10)]
        asked = []

        def pulls_for(sha):
            asked.append(sha)
            return [] if sha == shas[2] else [{'head': {'ref': 'repo-sync/ci/default'}}]

        self.assertEqual(promote.first_human_commit(shas, pulls_for), shas[2])
        self.assertEqual(asked, shas[:3])

    def test_newest_is_older_than_24h(self):
        old = '2026-09-18T06:00:00Z'
        young = '2026-09-19T12:00:00Z'
        self.assertTrue(promote.newest_is_older_than([old], NOW))
        self.assertFalse(promote.newest_is_older_than([old, young], NOW))
        self.assertTrue(promote.newest_is_older_than(['2026-09-19T06:00:00Z'], NOW))
        self.assertFalse(promote.newest_is_older_than(['2026-09-19T06:00:01Z'], NOW))
        self.assertFalse(promote.newest_is_older_than([], NOW))


class Evidence(unittest.TestCase):
    def test_soak_bearing_commit_is_the_nearest_built_ancestor(self):
        runs = {
            C: {'conclusion': 'success', 'finalize': False},
            B: {'conclusion': 'success', 'finalize': False},
            A: {'conclusion': 'success', 'finalize': True},
        }
        self.assertEqual(promote.soak_bearing_commit(C, [C, B, A], runs), A)

    def test_target_itself_when_built(self):
        runs = {C: {'conclusion': 'success', 'finalize': True}}
        self.assertEqual(promote.soak_bearing_commit(C, [C, B, A], runs), C)

    def test_failed_run_on_the_way_breaks_inheritance(self):
        runs = {
            C: {'conclusion': 'success', 'finalize': False},
            B: {'conclusion': 'failure', 'finalize': False},
            A: {'conclusion': 'success', 'finalize': True},
        }
        self.assertIsNone(promote.soak_bearing_commit(C, [C, B, A], runs))

    def test_run_less_commit_inherits_the_newer_push_heads_verdict(self):
        # B and A were pushed together with C (a rebase-merged pull request), so
        # only C has a run; its success covers them and the walk reaches D.
        runs = {
            C: {'conclusion': 'success', 'finalize': False},
            D: {'conclusion': 'success', 'finalize': True},
        }
        self.assertEqual(promote.soak_bearing_commit(C, [C, B, A, D], runs), D)

    def test_run_less_commit_after_a_failed_run_is_not_covered(self):
        runs = {
            C: {'conclusion': 'success', 'finalize': False},
            B: {'conclusion': 'failure', 'finalize': False},
            D: {'conclusion': 'success', 'finalize': True},
        }
        self.assertIsNone(promote.soak_bearing_commit(C, [C, B, A, D], runs))

    def test_run_less_target_is_never_covered(self):
        runs = {B: {'conclusion': 'success', 'finalize': True}}
        self.assertIsNone(promote.soak_bearing_commit(C, [C, B], runs))

    def test_chain_ending_without_a_build_finds_nothing(self):
        runs = {C: {'conclusion': 'success', 'finalize': False}}
        self.assertIsNone(promote.soak_bearing_commit(C, [C, B, A], runs))

    def test_chain_must_start_at_target(self):
        runs = {B: {'conclusion': 'success', 'finalize': True}}
        self.assertIsNone(promote.soak_bearing_commit(C, [B, A], runs))

    def test_walk_reaches_a_build_anywhere_inside_the_bound(self):
        chain = [f'{i:040x}' for i in range(promote.SOAK_WALK_COMMITS)]
        for built_at in (30, 31, promote.SOAK_WALK_COMMITS - 1):
            runs = {sha: {'conclusion': 'success', 'finalize': False} for sha in chain}
            runs[chain[built_at]] = {'conclusion': 'success', 'finalize': True}
            self.assertEqual(promote.soak_bearing_commit(chain[0], chain, runs), chain[built_at])

    def test_a_build_beyond_the_walk_bound_is_no_evidence(self):
        # docker-release.yaml's digest walk stops at the same count, so a build
        # further down could not be promoted either.
        chain = [f'{i:040x}' for i in range(promote.SOAK_WALK_COMMITS + 50)]
        walk = promote.soak_chain(chain, chain[0], chain[-1])
        self.assertEqual(len(walk), promote.SOAK_WALK_COMMITS + 1)
        runs = {sha: {'conclusion': 'success', 'finalize': False} for sha in chain}
        runs[chain[promote.SOAK_WALK_COMMITS + 20]] = {'conclusion': 'success', 'finalize': True}
        self.assertIsNone(promote.soak_bearing_commit(chain[0], walk, runs))

    def test_the_walk_ends_at_the_same_ancestor_as_the_docker_digest_walk(self):
        # docker-release.yaml checks the target and then `--skip=1 --max-count=200`
        # ancestors, so the build at ancestor 200 is one it would reuse and the
        # build at ancestor 201 is not; the soak walk must draw the line there.
        chain = [f'{i:040x}' for i in range(promote.SOAK_WALK_COMMITS + 5)]
        walk = promote.soak_chain(chain, chain[0], chain[-1])
        runs = {sha: {'conclusion': 'success', 'finalize': False} for sha in chain}
        runs[chain[promote.SOAK_WALK_COMMITS]] = {'conclusion': 'success', 'finalize': True}
        self.assertEqual(
            promote.soak_bearing_commit(chain[0], walk, runs), chain[promote.SOAK_WALK_COMMITS]
        )
        runs[chain[promote.SOAK_WALK_COMMITS]] = {'conclusion': 'success', 'finalize': False}
        runs[chain[promote.SOAK_WALK_COMMITS + 1]] = {'conclusion': 'success', 'finalize': True}
        self.assertIsNone(promote.soak_bearing_commit(chain[0], walk, runs))

    def test_soak_chain_runs_from_target_to_the_root_past_main(self):
        chain = [D, C, B, A]
        self.assertEqual(promote.soak_chain(chain, D, B), [D, C, B, A])
        self.assertEqual(promote.soak_chain(chain, C, C), [C, B, A])
        self.assertIsNone(promote.soak_chain(chain, C, 'e' * 40))
        self.assertIsNone(promote.soak_chain(chain, B, D))
        long_chain = [f'{i:040x}' for i in range(40)]
        self.assertEqual(promote.soak_chain(long_chain, long_chain[0], long_chain[39]), long_chain)
        self.assertEqual(promote.soak_chain(long_chain, long_chain[0], long_chain[20]), long_chain)

    def test_docs_only_target_above_a_docs_only_main_inherits_the_build_before_main(self):
        # T (target) and M (main) are both docs-only commits with successful
        # runs and no finalize job; B, immediately below main, built the image.
        t, m, b = D, C, B
        runs = {
            t: {'conclusion': 'success', 'finalize': False},
            m: {'conclusion': 'success', 'finalize': False},
            b: {'conclusion': 'success', 'finalize': True},
        }
        walk = promote.soak_chain([t, m, b, A], t, m)
        self.assertEqual(promote.soak_bearing_commit(t, walk, runs), b)

    def test_a_newer_no_op_run_does_not_hide_the_finalize_run_at_the_same_commit(self):
        # Run 2 is a later dispatch at the tagged commit: successful, no finalize
        # job. Run 1 built and finalized the image. The commit is built.
        runs = [{'conclusion': 'success', 'id': 2}, {'conclusion': 'success', 'id': 1}]
        jobs = {
            'repos/cplieger/knell/actions/runs/2/jobs?per_page=100': {
                'jobs': [{'name': 'release / detect', 'conclusion': 'success'}]
            },
            'repos/cplieger/knell/actions/runs/1/jobs?per_page=100': {
                'jobs': [
                    {'name': 'release / detect', 'conclusion': 'success'},
                    {'name': 'release / docker / finalize', 'conclusion': 'success'},
                ]
            },
        }
        asked = []
        originals = (promote.release_runs, promote.gh_json)
        promote.release_runs = lambda repo, sha: promote.RunListing(runs, complete=True)
        promote.gh_json = lambda path: asked.append(path) or jobs[path]
        try:
            self.assertEqual(
                promote.LazyRuns('knell').get(A), {'conclusion': 'success', 'finalize': True}
            )
        finally:
            promote.release_runs, promote.gh_json = originals
        self.assertEqual(asked, sorted(jobs, reverse=True))

    def test_a_commit_whose_successful_runs_all_lack_finalize_is_not_built(self):
        runs = [{'conclusion': 'success', 'id': 2}, {'conclusion': 'success', 'id': 1}]
        originals = (promote.release_runs, promote.gh_json)
        promote.release_runs = lambda repo, sha: promote.RunListing(runs, complete=True)
        promote.gh_json = lambda path: {
            'jobs': [{'name': 'release / docker / finalize', 'conclusion': 'skipped'}]
        }
        try:
            self.assertEqual(
                promote.LazyRuns('knell').get(A), {'conclusion': 'success', 'finalize': False}
            )
        finally:
            promote.release_runs, promote.gh_json = originals

    def test_a_failed_run_under_a_newer_no_op_run_ends_the_walk(self):
        # At the target, run 2 is a later successful dispatch with no docker
        # job and run 1 is the build attempt that failed; the parent built
        # cleanly. The target's own image may exist, so it inherits nothing.
        target, parent = D, C
        runs = {
            target: [{'conclusion': 'success', 'id': 2}, {'conclusion': 'failure', 'id': 1}],
            parent: [{'conclusion': 'success', 'id': 0}],
        }
        jobs = {
            'repos/cplieger/knell/actions/runs/2/jobs?per_page=100': {
                'jobs': [{'name': 'release / detect', 'conclusion': 'success'}]
            },
            'repos/cplieger/knell/actions/runs/0/jobs?per_page=100': {
                'jobs': [{'name': 'release / docker / finalize', 'conclusion': 'success'}]
            },
        }
        asked = []
        originals = (promote.release_runs, promote.gh_json)
        promote.release_runs = lambda repo, sha: promote.RunListing(runs[sha], complete=True)
        promote.gh_json = lambda path: asked.append(path) or jobs[path]
        try:
            lazy = promote.LazyRuns('knell')
            self.assertEqual(lazy.get(target), {'conclusion': 'failure', 'finalize': False})
            self.assertIsNone(promote.soak_bearing_commit(target, [target, parent], lazy))
        finally:
            promote.release_runs, promote.gh_json = originals
        self.assertEqual(asked, ['repos/cplieger/knell/actions/runs/2/jobs?per_page=100'])

    def test_a_finalized_build_is_evidence_beside_a_run_that_did_not_succeed(self):
        runs = [{'conclusion': None, 'id': 2}, {'conclusion': 'success', 'id': 1}]
        originals = (promote.release_runs, promote.gh_json)
        promote.release_runs = lambda repo, sha: promote.RunListing(runs, complete=True)
        promote.gh_json = lambda path: {
            'jobs': [{'name': 'release / docker / finalize', 'conclusion': 'success'}]
        }
        try:
            self.assertEqual(
                promote.LazyRuns('knell').get(A), {'conclusion': 'success', 'finalize': True}
            )
        finally:
            promote.release_runs, promote.gh_json = originals

    def test_a_commit_with_no_release_run_is_unknown_and_reads_no_jobs(self):
        asked = []
        originals = (promote.release_runs, promote.gh_json)
        promote.release_runs = lambda repo, sha: promote.RunListing([], complete=True)
        promote.gh_json = asked.append
        try:
            self.assertIsNone(promote.LazyRuns('knell').get(A))
        finally:
            promote.release_runs, promote.gh_json = originals
        self.assertEqual(asked, [])

    RUNS_PATH = f'repos/cplieger/knell/actions/workflows/release.yaml/runs?branch=dev&head_sha={A}'

    def test_a_finalized_build_on_the_second_page_of_runs_is_evidence(self):
        # A full first page of later no-op dispatches at the tagged commit;
        # the run that built the image is older and sits on page two.
        pages = {
            1: [{'conclusion': 'success', 'id': i} for i in range(101, 1, -1)],
            2: [{'conclusion': 'success', 'id': 1}],
        }
        asked = []

        def fake_gh_json(path):
            asked.append(path)
            if '/jobs?' in path:
                built = path.endswith('/runs/1/jobs?per_page=100')
                return {
                    'jobs': [
                        {
                            'name': 'release / docker / finalize' if built else 'release / detect',
                            'conclusion': 'success',
                        }
                    ]
                }
            return {'workflow_runs': pages[int(path.rsplit('&page=', 1)[1])]}

        original = promote.gh_json
        promote.gh_json = fake_gh_json
        try:
            self.assertEqual(
                promote.LazyRuns('knell').get(A), {'conclusion': 'success', 'finalize': True}
            )
        finally:
            promote.gh_json = original
        self.assertEqual(
            [a for a in asked if '/jobs?' not in a],
            [f'{self.RUNS_PATH}&per_page=100&page={n}' for n in (1, 2)],
        )

    def test_a_run_listing_that_fills_every_page_is_a_failure_that_ends_the_walk(self):
        # Five hundred successful no-op runs and no finalized build among them:
        # the build, or a failed attempt, may sit beyond the last page read.
        target, parent = A, B
        asked = []

        def fake_gh_json(path):
            asked.append(path)
            if '/jobs?' in path:
                return {'jobs': [{'name': 'release / detect', 'conclusion': 'success'}]}
            page = int(path.rsplit('&page=', 1)[1])
            return {
                'workflow_runs': [
                    {'conclusion': 'success', 'id': 1000 - 100 * page - i} for i in range(100)
                ]
            }

        original = promote.gh_json
        promote.gh_json = fake_gh_json
        try:
            lazy = promote.LazyRuns('knell')
            self.assertEqual(lazy.get(target), {'conclusion': 'failure', 'finalize': False})
            lazy.cache[parent] = {'conclusion': 'success', 'finalize': True}
            self.assertIsNone(promote.soak_bearing_commit(target, [target, parent], lazy))
        finally:
            promote.gh_json = original
        self.assertEqual(
            [a for a in asked if '/jobs?' not in a],
            [f'{self.RUNS_PATH}&per_page=100&page={n}' for n in range(1, 6)],
        )

    def test_evidence_ok_needs_a_successful_run(self):
        self.assertTrue(promote.evidence_ok([{'conclusion': 'failure'}, {'conclusion': 'success'}]))
        self.assertFalse(promote.evidence_ok([{'conclusion': 'failure'}]))
        self.assertFalse(promote.evidence_ok([]))

    def test_soak_override_status_carries_the_reason_within_the_api_limit(self):
        status = promote.soak_override_status(
            'success', 'hotfix:  CVE-2026-1 in the base image', 'https://run'
        )
        self.assertEqual(status['context'], 'promotion/soak-override')
        self.assertEqual(status['state'], 'success')
        self.assertEqual(status['description'], 'hotfix: CVE-2026-1 in the base image')
        self.assertEqual(status['target_url'], 'https://run')
        long = promote.soak_override_status('pending', 'x' * 200, 'https://run')['description']
        self.assertEqual(len(long), 140)
        self.assertTrue(long.endswith('...'))

    def test_soak_ok_reads_only_the_soak_context(self):
        self.assertTrue(promote.soak_ok([{'context': 'homelab/soak', 'state': 'success'}]))
        self.assertFalse(promote.soak_ok([{'context': 'homelab/soak', 'state': 'pending'}]))
        self.assertFalse(promote.soak_ok([{'context': 'ci / validate', 'state': 'success'}]))
        self.assertEqual(promote.soak_state([]), 'missing')
        self.assertEqual(
            promote.soak_state([{'context': 'homelab/soak', 'state': 'failure'}]), 'failure'
        )


class Opts:
    scheduled = False
    skip_soak = False
    reason = ''


class PlanRepo(unittest.TestCase):
    """plan_repo against fakes of every GitHub and git read it makes."""

    NAMES = (
        'repo_default_branch',
        'clone_repo',
        'git',
        'compare',
        'delta_commits',
        'files_at',
        'release_runs',
        'commit_statuses',
    )

    def setUp(self):
        self.originals = {n: getattr(promote, n) for n in self.NAMES}
        self.asked = []
        promote.repo_default_branch = lambda repo: 'dev'
        promote.clone_repo = lambda repo, work_dir: __import__('pathlib').Path('/nonexistent')
        promote.git = self.fake_git
        promote.compare = lambda repo, base, head: {'status': 'ahead', 'ahead_by': 1}
        promote.delta_commits = lambda clone, target: ([B], ['2026-09-18T06:00:00Z'])
        promote.files_at = lambda clone, target: {}
        promote.release_runs = self.fake_release_runs
        promote.commit_statuses = lambda repo, sha: [
            {'context': 'homelab/soak', 'state': 'success'}
        ]

    def tearDown(self):
        for name, fn in self.originals.items():
            setattr(promote, name, fn)

    def fake_git(self, clone, *args):
        if args[:2] == ('rev-parse', 'origin/dev'):
            return B + '\n'
        if args[:2] == ('rev-parse', 'origin/main'):
            return A + '\n'
        if args[0] == 'tag':
            return 'v1.0.0\n'
        if args[0] == 'rev-list':
            return f'{B}\n{A}\n'
        raise AssertionError(args)

    def fake_release_runs(self, repo, sha):
        self.asked.append(('release_runs', repo, sha))
        return promote.RunListing([{'conclusion': 'success', 'id': 1}], complete=True)

    def test_an_own_publish_repo_skips_the_release_run_read(self):
        row = promote.plan_repo('web-terminal-glyphs', None, Opts(), None, NOW)
        self.assertEqual(row['decision'], 'promote', row)
        self.assertEqual(self.asked, [])
        self.assertTrue(any('own workflow' in r for r in row['reasons']), row['reasons'])

    def test_a_central_release_repo_reads_the_release_run(self):
        row = promote.plan_repo('httpx', None, Opts(), None, NOW)
        self.assertEqual(row['decision'], 'promote', row)
        self.assertEqual(self.asked, [('release_runs', 'httpx', B)])

    def test_a_deployed_repo_records_the_commit_that_carried_the_soak(self):
        row = promote.plan_repo('knell', None, Opts(), None, NOW)
        self.assertEqual(row['decision'], 'promote', row)
        self.assertEqual(row['soak_commit'], B)
        self.assertEqual(row['soak_skipped_reason'], '')

    def test_a_library_records_no_soak_commit(self):
        row = promote.plan_repo('httpx', None, Opts(), None, NOW)
        self.assertEqual(row['soak_commit'], '')

    def test_skip_soak_reason_is_recorded_on_every_row_and_overrides_only_a_deployed_one(self):
        opts = Opts()
        opts.skip_soak = True
        opts.reason = 'hotfix'
        deployed = promote.plan_repo('knell', None, opts, None, NOW)
        library = promote.plan_repo('httpx', None, opts, None, NOW)
        self.assertEqual(deployed['soak_skipped_reason'], 'hotfix')
        self.assertEqual(library['soak_skipped_reason'], '')
        for row in (deployed, library):
            self.assertTrue(
                any('Soak check skipped by request: hotfix' in r for r in row['reasons']), row
            )
        self.assertTrue(any('No soak is required' in r for r in library['reasons']), library)
        self.assertEqual(deployed['soak_commit'], '')


class Refusals(unittest.TestCase):
    def test_single_main_repo_is_refused_before_any_read(self):
        row = promote.plan_repo('ci', None, Opts(), None, NOW)
        self.assertEqual(row['decision'], 'blocked')
        self.assertIn('no dev channel', row['reasons'][0])

    def test_repo_still_on_main_is_refused(self):
        original = promote.repo_default_branch
        promote.repo_default_branch = lambda repo: 'main'
        try:
            row = promote.plan_repo('knell', None, Opts(), None, NOW)
        finally:
            promote.repo_default_branch = original
        self.assertEqual(row['decision'], 'blocked')
        self.assertIn('not dev', row['reasons'][0])

    def test_scheduled_candidates_filter(self):
        listing = [
            {
                'name': 'knell',
                'isArchived': False,
                'isFork': False,
                'visibility': 'PUBLIC',
                'defaultBranchRef': {'name': 'dev'},
            },
            {
                'name': 'ci',
                'isArchived': False,
                'isFork': False,
                'visibility': 'PUBLIC',
                'defaultBranchRef': {'name': 'dev'},
            },
            {
                'name': 'httpx',
                'isArchived': False,
                'isFork': False,
                'visibility': 'PUBLIC',
                'defaultBranchRef': {'name': 'main'},
            },
            {
                'name': 'infra',
                'isArchived': False,
                'isFork': False,
                'visibility': 'PRIVATE',
                'defaultBranchRef': {'name': 'dev'},
            },
            {
                'name': 'loki',
                'isArchived': False,
                'isFork': True,
                'visibility': 'PUBLIC',
                'defaultBranchRef': {'name': 'dev'},
            },
            {
                'name': 'iwebkit',
                'isArchived': True,
                'isFork': False,
                'visibility': 'PUBLIC',
                'defaultBranchRef': {'name': 'dev'},
            },
            {
                'name': 'arrapi',
                'isArchived': False,
                'isFork': False,
                'visibility': 'PUBLIC',
                'defaultBranchRef': {'name': 'dev'},
            },
        ]
        original = promote.gh
        promote.gh = lambda *args: __import__('json').dumps(listing)
        try:
            self.assertEqual(promote.scheduled_candidates(), ['arrapi', 'knell'])
        finally:
            promote.gh = original


class Summary(unittest.TestCase):
    def test_render_lists_each_repo_with_its_reasons(self):
        rows = [
            {
                'repo': 'knell',
                'target': A,
                'decision': 'promote',
                'reasons': ['Every check passed.'],
                'last_stable': 'v2.0.17',
            },
            {'repo': 'httpx', 'target': B, 'decision': 'blocked', 'reasons': ['x', 'y']},
        ]
        text = promote.render_summary(rows)
        self.assertIn('**knell** at `aaaaaaaaaaaa`: promote (last stable v2.0.17).', text)
        self.assertIn('**httpx** at `bbbbbbbbbbbb`: blocked.', text)
        self.assertIn('  - x\n  - y', text)

    def test_render_empty(self):
        self.assertIn('No repository was considered.', promote.render_summary([]))

    def test_blocked_title_is_stable_per_failure_class(self):
        self.assertEqual(promote.blocked_title({'blocked_by': 'soak'}), promote.SOAK_BLOCKED_TITLE)
        self.assertEqual(
            promote.blocked_title(
                {'blocked_by': 'evidence', 'reasons': [f'The dev release run at {A} failed']}
            ),
            'Promotion blocked: the dev release run has not succeeded',
        )
        self.assertEqual(
            promote.blocked_title({'blocked_by': 'purity'}),
            'Promotion blocked: a first-party dependency is pinned at a dev version',
        )
        self.assertEqual(promote.blocked_title({}), 'Promotion blocked: a check failed')
        for title in promote.BLOCKED_TITLES.values():
            self.assertNotRegex(title, '[0-9a-f]{12}')


class Apply(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self.soak_now = [{'context': 'homelab/soak', 'state': 'success'}]
        self.originals = (
            promote.post_status,
            promote.fast_forward,
            promote.gh,
            promote.commit_statuses,
        )
        promote.post_status = lambda repo, sha, status: self.calls.append(
            ('status', repo, sha, status)
        )
        promote.fast_forward = lambda repo, sha: self.calls.append(('ff', repo, sha))
        promote.commit_statuses = lambda repo, sha: (
            self.calls.append(('soak', repo, sha)) or list(self.soak_now)
        )

    def tearDown(self):
        promote.post_status, promote.fast_forward, promote.gh, promote.commit_statuses = (
            self.originals
        )
        self.tmp.cleanup()

    def run_apply(self, rows):
        import json
        from pathlib import Path

        plan = Path(self.tmp.name) / 'plan.json'
        plan.write_text(json.dumps(rows))
        return promote.main(['--work-dir', self.tmp.name, 'apply', '--run-url', 'https://run'])

    def override_row(self) -> dict:
        return {
            'repo': 'knell',
            'target': A,
            'decision': 'promote',
            'reasons': [],
            'mode': 'manual',
            'soak_skipped_reason': 'hotfix for CVE-2026-1',
        }

    def normal_row(self) -> dict:
        return {
            'repo': 'knell',
            'target': A,
            'decision': 'promote',
            'reasons': [],
            'mode': 'manual',
            'soak_commit': B,
        }

    def refusing_fast_forward(self, message: str):
        def refuse(repo, sha):
            self.calls.append(('ff', repo, sha))
            raise promote.GhError(message)

        return refuse

    def test_soak_override_is_recorded_as_success_before_main_moves(self):
        rc_ = self.run_apply([self.override_row()])
        self.assertEqual(rc_, 0)
        self.assertEqual([c[0] for c in self.calls], ['status', 'ff'])
        status = self.calls[0]
        self.assertEqual(status[1:3], ('knell', A))
        self.assertEqual(status[3]['state'], 'success')
        self.assertEqual(status[3]['context'], 'promotion/soak-override')
        self.assertEqual(status[3]['description'], 'hotfix for CVE-2026-1')
        self.assertEqual(status[3]['target_url'], 'https://run')

    def test_a_normal_promotion_records_an_empty_success_over_a_failed_override(self):
        # The failed attempt left its failure status on the target; the retry
        # after a real soak writes a success with no reason, so the newest row
        # the stable notes read carries nothing to repeat.
        promote.fast_forward = self.refusing_fast_forward('main moved since planning')
        self.run_apply([self.override_row()])
        promote.fast_forward = lambda repo, sha: self.calls.append(('ff', repo, sha))
        rc_ = self.run_apply([self.normal_row()])
        self.assertEqual(rc_, 0)
        statuses = [c[3] for c in self.calls if c[0] == 'status']
        self.assertEqual([s['state'] for s in statuses], ['success', 'failure', 'success'])
        self.assertEqual(statuses[2]['description'], '')
        self.assertEqual([c[0] for c in self.calls[3:]], ['soak', 'status', 'ff'])

    def test_a_failed_fast_forward_turns_the_override_into_a_failure(self):
        promote.fast_forward = self.refusing_fast_forward(
            'main moved since planning (compare status diverged)'
        )
        rc_ = self.run_apply([self.override_row()])
        self.assertEqual(rc_, 1)
        self.assertEqual([c[0] for c in self.calls], ['status', 'ff', 'status'])
        self.assertEqual(self.calls[0][3]['state'], 'success')
        self.assertEqual(self.calls[2][3]['state'], 'failure')
        self.assertIn('main moved since planning', self.calls[2][3]['description'])
        self.assertEqual(self.calls[2][3]['context'], 'promotion/soak-override')

    def test_a_failed_fast_forward_turns_a_normal_record_into_a_failure(self):
        promote.fast_forward = self.refusing_fast_forward('main moved since planning')
        rc_ = self.run_apply([self.normal_row()])
        self.assertEqual(rc_, 1)
        self.assertEqual([c[0] for c in self.calls], ['soak', 'status', 'ff', 'status'])
        self.assertEqual(
            self.calls[1][3], promote.soak_override_status('success', '', 'https://run')
        )
        self.assertEqual(self.calls[3][3]['state'], 'failure')
        self.assertIn('main moved since planning', self.calls[3][3]['description'])

    def test_a_record_that_cannot_be_written_stops_the_promotion_before_main_moves(self):
        def refuse(repo, sha, status):
            self.calls.append(('status', repo, sha, status))
            raise promote.GhError('gh api -X POST failed: HTTP 502')

        promote.post_status = refuse
        rc_ = self.run_apply([self.override_row()])
        self.assertEqual(rc_, 1)
        self.assertEqual([c[0] for c in self.calls], ['status'])
        import json
        from pathlib import Path

        result = json.loads(Path(self.tmp.name, 'result.json').read_text())[0]
        self.assertFalse(result['promoted'])
        self.assertIn('HTTP 502', result['error'])

    def test_soak_is_read_again_on_its_commit_right_before_main_moves(self):
        rc_ = self.run_apply([self.normal_row()])
        self.assertEqual(rc_, 0)
        self.assertEqual(
            [c[:3] for c in self.calls],
            [('soak', 'knell', B), ('status', 'knell', A), ('ff', 'knell', A)],
        )

    def test_a_soak_revoked_since_planning_stops_the_fast_forward(self):
        self.soak_now[0]['state'] = 'failure'
        rc_ = self.run_apply(
            [
                {
                    'repo': 'knell',
                    'target': A,
                    'decision': 'promote',
                    'reasons': [],
                    'mode': 'manual',
                    'soak_commit': B,
                }
            ]
        )
        self.assertEqual(rc_, 1)
        self.assertEqual([c[0] for c in self.calls], ['soak'])
        import json
        from pathlib import Path

        result = json.loads(Path(self.tmp.name, 'result.json').read_text())[0]
        self.assertFalse(result['promoted'])
        self.assertIn('is failure now', result['error'])

    def test_a_recorded_override_skips_the_soak_re_read(self):
        rc_ = self.run_apply(
            [
                {
                    'repo': 'knell',
                    'target': A,
                    'decision': 'promote',
                    'reasons': [],
                    'mode': 'manual',
                    'soak_commit': '',
                    'soak_skipped_reason': 'hotfix',
                }
            ]
        )
        self.assertEqual(rc_, 0)
        self.assertEqual([c[0] for c in self.calls], ['status', 'ff'])

    def test_manual_refusal_is_a_red_run(self):
        rc_ = self.run_apply(
            [
                {
                    'repo': 'knell',
                    'target': A,
                    'decision': 'blocked',
                    'reasons': ['x'],
                    'mode': 'manual',
                }
            ]
        )
        self.assertEqual(rc_, 1)
        self.assertEqual(self.calls, [])

    def test_manual_no_op_is_not_a_refusal(self):
        rc_ = self.run_apply(
            [
                {
                    'repo': 'knell',
                    'target': A,
                    'decision': 'skip',
                    'reasons': ['nothing to promote'],
                    'mode': 'manual',
                }
            ]
        )
        self.assertEqual(rc_, 0)
        self.assertEqual(self.calls, [])

    def test_scheduled_skip_is_quiet(self):
        rc_ = self.run_apply(
            [
                {
                    'repo': 'knell',
                    'target': A,
                    'decision': 'blocked',
                    'reasons': ['x'],
                    'mode': 'scheduled',
                }
            ]
        )
        self.assertEqual(rc_, 0)


class Report(unittest.TestCase):
    def test_a_failed_issue_listing_is_reported_not_swallowed(self):
        import json
        import tempfile
        from pathlib import Path

        original = promote.gh

        def failing_gh(*args):
            raise promote.GhError('gh issue list failed: HTTP 502')

        promote.gh = failing_gh
        try:
            with tempfile.TemporaryDirectory() as tmp:
                Path(tmp, 'plan.json').write_text(
                    json.dumps(
                        [
                            {
                                'repo': 'knell',
                                'target': A,
                                'decision': 'promote',
                                'reasons': [],
                                'mode': 'manual',
                            }
                        ]
                    )
                )
                Path(tmp, 'result.json').write_text(
                    json.dumps([{'repo': 'knell', 'target': A, 'promoted': True, 'error': ''}])
                )
                rc_ = promote.main(['--work-dir', tmp, 'report', '--run-url', 'https://run'])
        finally:
            promote.gh = original
        self.assertEqual(rc_, 1)

    def test_a_missing_plan_is_one_sentence_not_a_traceback(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            rc_ = promote.main(['--work-dir', tmp, 'report', '--run-url', 'https://run'])
        self.assertEqual(rc_, 1)


if __name__ == '__main__':
    unittest.main()
