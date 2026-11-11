#!/usr/bin/env python3
"""Pin the contract of the gremlins measurement scripts; exit 0 = pass.

gremlins-scope.py writes each module's config with its deeper modules excluded,
gremlins-merge.py folds one result per Go module into one per-attempt result,
gremlins-aggregate.py turns the attempts into the tracker body, history and
badge, and gremlins-run.sh's coefficient and mode decision are extracted and
run. Nothing else executes them before the weekly run publishes their number.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_spec = importlib.util.spec_from_file_location('gremlins_aggregate', HERE / 'gremlins-aggregate.py')
gremlins_aggregate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gremlins_aggregate)
MERGE = HERE / 'gremlins-merge.py'
AGGREGATE = HERE / 'gremlins-aggregate.py'
RUNNER = HERE / 'gremlins-run.sh'
SCOPE = HERE / 'gremlins-scope.py'
SHARD = HERE / 'gremlins-shard.py'
CONTAINER = HERE / 'gremlins-container.sh'
PREPARE = HERE / 'gremlins-prepare.sh'
WORKFLOW = HERE.parent / '.github' / 'workflows' / 'weekly-gremlins.yaml'
CENTRAL = HERE.parent / 'configs' / 'gremlins.yaml'

FAILURES: list[str] = []


def check(name: str, *, ok: bool, detail: str = '') -> None:
    if ok:
        print(f'  PASS  {name}')
    else:
        print(f'  FAIL  {name}{": " + detail if detail else ""}')
        FAILURES.append(name)


def result(module: str, files: list[tuple[str, list[tuple[str, str]]]]) -> dict:
    """Build a gremlins v0.6.0 --output document with self-consistent totals.

    files: [(file_name, [(status, mutator_type), ...]), ...]

    The counters and percentages follow internal/report: mutants_total is
    killed+lived+not_viable (TIMED OUT / SKIPPED are in no counter), efficacy is
    killed/(killed+lived) and 0 when nothing was killed, coverage is
    (killed+lived)/(killed+lived+not_covered).
    """
    stat_key = {
        'ARITHMETIC_BASE': 'arithmetic_base',
        'CONDITIONALS_BOUNDARY': 'conditionals_boundary',
        'CONDITIONALS_NEGATION': 'conditionals_negation',
        'INVERT_BITWISE': 'invert_bitwise',
        'INVERT_BWASSIGN': 'invert_bitwise_assignments',
        'INVERT_LOGICAL': 'invert_logical',
        'INVERT_LOOPCTRL': 'invert_loop_ctrl',
    }
    counts = {'KILLED': 0, 'LIVED': 0, 'NOT COVERED': 0, 'NOT VIABLE': 0, 'TIMED OUT': 0}
    stats: dict[str, int] = {}
    out_files = []
    for name, muts in files:
        out_files.append({
            'file_name': name,
            'mutations': [
                {'type': t, 'status': s, 'line': 10 + i, 'column': 3}
                for i, (s, t) in enumerate(muts)
            ],
        })
        for s, t in muts:
            counts[s] += 1
            stats[stat_key[t]] = stats.get(stat_key[t], 0) + 1
    killed, lived = counts['KILLED'], counts['LIVED']
    nc, nv = counts['NOT COVERED'], counts['NOT VIABLE']
    return {
        'go_module': module,
        'test_efficacy': (killed / (killed + lived) * 100) if killed else 0.0,
        'mutations_coverage': ((killed + lived) / (killed + lived + nc) * 100) if killed + lived else 0.0,
        'mutants_total': killed + lived + nv,
        'mutants_killed': killed,
        'mutants_lived': lived,
        'mutants_not_viable': nv,
        'mutants_not_covered': nc,
        'elapsed_time': 12.5,
        'mutator_statistics': stats,
        'files': out_files,
    }


def run_merge(tmp: Path, modules: list[tuple[str, dict | None]], out_name: str = 'merged.json',
              modes: dict[str, str] | None = None, skip: tuple[str, ...] = ()):
    """Write each module's JSON (None = no file at all) and merge them.

    modes: {dir: text} written as the `<result>.mode` file gremlins-run.sh leaves.
    skip: module dirs passed as --skip-module (not run in this shard).
    """
    args = [arg for d in skip for arg in ('--skip-module', d)]
    for dir_, data in modules:
        path = tmp / (f'{dir_.replace("/", "_")}-out.json' if dir_ != '.' else 'root-out.json')
        if data is not None:
            path.write_text(json.dumps(data))
        if modes and dir_ in modes:
            Path(f'{path}.mode').write_text(modes[dir_])
        args += ['--module', f'{dir_}={path}']
    out = tmp / out_name
    proc = subprocess.run(
        [sys.executable, str(MERGE), '--out', str(out), *args],
        capture_output=True, text=True, check=False,
    )
    merged = json.loads(out.read_text()) if proc.returncode == 0 and out.exists() else None
    return proc, merged


# ---------------------------------------------------------------------------
# gremlins-merge.py
# ---------------------------------------------------------------------------
def test_single_module_is_identity(tmp: Path) -> None:
    """A repo with no nested module must publish exactly what gremlins said.

    ~40 cplieger repos are single-module, so this is the path the merge
    takes almost every time: it must not shift a single number.
    """
    root = result('github.com/cplieger/thing/v2', [
        ('thing.go', [('KILLED', 'ARITHMETIC_BASE'), ('LIVED', 'CONDITIONALS_BOUNDARY'),
                      ('NOT COVERED', 'INVERT_LOOPCTRL'), ('NOT VIABLE', 'INVERT_BWASSIGN')]),
        ('other.go', [('KILLED', 'CONDITIONALS_NEGATION'), ('TIMED OUT', 'ARITHMETIC_BASE')]),
    ])
    proc, merged = run_merge(tmp, [('.', root)])
    check('single module: merge succeeds', ok=proc.returncode == 0, detail=proc.stderr)
    if merged is None:
        return
    same = all(
        merged[k] == root[k]
        for k in ('go_module', 'mutants_total', 'mutants_killed', 'mutants_lived',
                  'mutants_not_viable', 'mutants_not_covered', 'mutator_statistics')
    )
    check('single module: counters unchanged', ok=same,
          detail=f'{ {k: merged[k] for k in ("mutants_total", "mutants_killed")} }')
    check('single module: efficacy unchanged',
          ok=abs(merged['test_efficacy'] - root['test_efficacy']) < 0.001,
          detail=f'{merged["test_efficacy"]} vs {root["test_efficacy"]}')
    check('single module: coverage unchanged',
          ok=abs(merged['mutations_coverage'] - root['mutations_coverage']) < 0.001,
          detail=f'{merged["mutations_coverage"]} vs {root["mutations_coverage"]}')
    check('single module: mutation positions preserved',
          ok=sorted(f['file_name'] for f in merged['files']) == ['other.go', 'thing.go'])


def default_mutators_config(tmp: Path) -> tuple[str, ...]:
    """--config args for a config that runs gremlins' default mutators, as legacy rows did."""
    cfg = tmp / 'default-mutators.yaml'
    cfg.write_text('silent: false\n')
    return ('--config', str(cfg))


def attempt_dir(art: Path, repo: str, attempt: int) -> Path:
    """Stage one downloaded attempt artifact: the directory AND its meta.json.

    The aggregate script attributes an attempt to a repo by reading the meta.json
    the artifact carries, never by parsing the directory name, because
    actions/download-artifact only creates that directory when more than one
    artifact matched its pattern. Fixtures must carry the file for the same reason
    the real upload does.
    """
    d = art / f'gremlins-{repo}-{attempt}'
    d.mkdir(parents=True)
    (d / 'meta.json').write_text(json.dumps({'repo': repo, 'attempt': str(attempt)}))
    return d


def test_upstream_fixture_roundtrips(tmp: Path) -> None:
    """The recompute must reproduce a REAL gremlins document, not just ours.

    Values are gremlins v0.6.0's own internal/report/testdata/normal_output.json:
    4 killed, 3 lived, 2 not viable, 3 not covered -> 57.142857% efficacy, 70%
    coverage. If gremlins ever changes how it counts, this fails here rather
    than publishing a number nobody checked.
    """
    upstream = {
        'go_module': 'example.com/go/module',
        'test_efficacy': 57.14285714285714,
        'mutations_coverage': 70,
        'mutants_total': 9,
        'mutants_killed': 4,
        'mutants_lived': 3,
        'mutants_not_viable': 2,
        'mutants_not_covered': 3,
        'elapsed_time': 142.123,
        'mutator_statistics': {'arithmetic_base': 1, 'conditionals_boundary': 1},
        'files': [
            {'file_name': 'file1.go', 'mutations': [
                {'type': 'ARITHMETIC_BASE', 'status': 'KILLED', 'line': 1, 'column': 1},
                {'type': 'CONDITIONALS_BOUNDARY', 'status': 'KILLED', 'line': 2, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'LIVED', 'line': 3, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'NOT COVERED', 'line': 4, 'column': 1},
            ]},
            {'file_name': 'file2.go', 'mutations': [
                {'type': 'ARITHMETIC_BASE', 'status': 'KILLED', 'line': 1, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'KILLED', 'line': 2, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'LIVED', 'line': 3, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'LIVED', 'line': 4, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'NOT COVERED', 'line': 5, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'NOT VIABLE', 'line': 6, 'column': 1},
            ]},
            {'file_name': 'file3.go', 'mutations': [
                {'type': 'ARITHMETIC_BASE', 'status': 'NOT COVERED', 'line': 1, 'column': 1},
                {'type': 'ARITHMETIC_BASE', 'status': 'NOT VIABLE', 'line': 2, 'column': 1},
            ]},
        ],
    }
    # mutator_statistics is asserted separately by the self-check; strip it so
    # this case pins the counters and percentages only.
    del upstream['mutator_statistics']
    proc, merged = run_merge(tmp, [('.', upstream)], out_name='upstream.json')
    check('upstream fixture: merge succeeds', ok=proc.returncode == 0, detail=proc.stderr)
    if merged is None:
        return
    check('upstream fixture: efficacy 57.14%',
          ok=abs(merged['test_efficacy'] - 57.14285714285714) < 0.001,
          detail=str(merged['test_efficacy']))
    check('upstream fixture: coverage 70%',
          ok=abs(merged['mutations_coverage'] - 70) < 0.001, detail=str(merged['mutations_coverage']))
    check('upstream fixture: total 9', ok=merged['mutants_total'] == 9, detail=str(merged['mutants_total']))


def test_nested_module_is_measured_not_counted_uncovered(tmp: Path) -> None:
    """The envx defect, end to end.

    Root run: 5 killed / 1 lived of its own, plus a nested module's 4 files
    analysed and uncovered because a root `go test ./...` cannot run them.
    Nested run: the same code measured by its own suite, 4 killed.

    Merged, the nested mutants must count as killed (not as uncovered noise),
    the root's copies must be dropped exactly once, and paths must stay
    repo-relative.
    """
    root = result('github.com/cplieger/envx/v2', [
        ('envx.go', [('KILLED', 'ARITHMETIC_BASE'), ('KILLED', 'CONDITIONALS_BOUNDARY'),
                     ('KILLED', 'CONDITIONALS_NEGATION'), ('KILLED', 'INVERT_LOOPCTRL'),
                     ('KILLED', 'INVERT_BWASSIGN'), ('LIVED', 'ARITHMETIC_BASE')]),
        ('yamlenv/yamlenv.go', [('NOT COVERED', 'ARITHMETIC_BASE')] * 3),
        ('yamlenv/parse.go', [('NOT COVERED', 'CONDITIONALS_BOUNDARY')]),
    ])
    nested = result('github.com/cplieger/envx/yamlenv/v2', [
        ('yamlenv.go', [('KILLED', 'ARITHMETIC_BASE')] * 3),
        ('parse.go', [('KILLED', 'CONDITIONALS_BOUNDARY')]),
    ])
    check('nested: root run alone reports 60% coverage (the defect)',
          ok=abs(root['mutations_coverage'] - 60.0) < 0.001, detail=str(root['mutations_coverage']))

    proc, merged = run_merge(tmp, [('.', root), ('yamlenv', nested)], out_name='nested.json')
    check('nested: merge succeeds', ok=proc.returncode == 0, detail=proc.stderr)
    if merged is None:
        return
    check('nested: merged coverage is 100% (no module-boundary uncovered)',
          ok=abs(merged['mutations_coverage'] - 100.0) < 0.001, detail=str(merged['mutations_coverage']))
    check('nested: merged efficacy is weighted, not averaged',
          ok=abs(merged['test_efficacy'] - 9 / 10 * 100) < 0.001, detail=str(merged['test_efficacy']))
    check('nested: killed = 5 root + 4 nested', ok=merged['mutants_killed'] == 9,
          detail=str(merged['mutants_killed']))
    check('nested: no uncovered mutants survive the merge', ok=merged['mutants_not_covered'] == 0,
          detail=str(merged['mutants_not_covered']))
    names = sorted(f['file_name'] for f in merged['files'])
    check('nested: paths stay repo-relative and unduplicated',
          ok=names == ['envx.go', 'yamlenv/parse.go', 'yamlenv/yamlenv.go'], detail=str(names))
    dropped = {m['dir']: m['dropped_nested_mutations'] for m in merged['modules']}
    check('nested: per-module split records the 4 dropped root copies',
          ok=dropped == {'.': 4, 'yamlenv': 0}, detail=str(dropped))


def test_nested_verdict_in_root_run_is_fatal(tmp: Path) -> None:
    """Dropping a nested file that carries a real verdict must fail loudly.

    The drop is only safe because an uncovered mutant is never executed. If a
    root run ever DOES kill a mutant in a nested module, silently dropping it
    would bias the number, so the attempt has to go red instead.
    """
    root = result('github.com/cplieger/envx/v2', [
        ('envx.go', [('KILLED', 'ARITHMETIC_BASE')]),
        ('yamlenv/yamlenv.go', [('KILLED', 'ARITHMETIC_BASE')]),
    ])
    nested = result('github.com/cplieger/envx/yamlenv/v2', [
        ('yamlenv.go', [('KILLED', 'ARITHMETIC_BASE')]),
    ])
    proc, _ = run_merge(tmp, [('.', root), ('yamlenv', nested)], out_name='verdict.json')
    check('nested verdict in root run: exits non-zero', ok=proc.returncode != 0)
    check('nested verdict in root run: says which file and status',
          ok='yamlenv/yamlenv.go' in proc.stderr and 'KILLED' in proc.stderr,
          detail=proc.stderr[-300:])
    check('nested verdict in root run: blames the scope exclusion',
          ok='gremlins-scope.py' in proc.stderr, detail=proc.stderr[-300:])


def test_inconsistent_input_is_fatal(tmp: Path) -> None:
    """A gremlins result whose own totals disagree with its mutant list.

    This is the schema-drift guard: it fires if a future gremlins version counts
    differently, instead of publishing a number derived from a formula that no
    longer matches the tool.
    """
    root = result('github.com/cplieger/thing/v2', [
        ('thing.go', [('KILLED', 'ARITHMETIC_BASE'), ('LIVED', 'ARITHMETIC_BASE')]),
    ])
    root['mutants_killed'] = 7
    proc, _ = run_merge(tmp, [('.', root)], out_name='inconsistent.json')
    check('inconsistent input: exits non-zero', ok=proc.returncode != 0)
    check('inconsistent input: names the field',
          ok='mutants_killed' in proc.stderr, detail=proc.stderr[-300:])


def test_missing_module_output_is_zero_not_error(tmp: Path) -> None:
    """gremlins writes no file when a module has no mutants at all."""
    root = result('github.com/cplieger/thing/v2', [
        ('thing.go', [('KILLED', 'ARITHMETIC_BASE')]),
    ])
    proc, merged = run_merge(tmp, [('.', root), ('tools', None)], out_name='missing.json')
    check('missing module output: merge succeeds', ok=proc.returncode == 0, detail=proc.stderr)
    if merged is None:
        return
    check('missing module output: contributes nothing', ok=merged['mutants_total'] == 1,
          detail=str(merged['mutants_total']))
    check('missing module output: still listed in the split',
          ok=[m['dir'] for m in merged['modules']] == ['.', 'tools'])


def test_module_nested_in_nested(tmp: Path) -> None:
    """Three levels: each mutant must land in exactly one bucket."""
    root = result('github.com/cplieger/thing/v2', [
        ('thing.go', [('KILLED', 'ARITHMETIC_BASE')]),
        ('a/a.go', [('NOT COVERED', 'ARITHMETIC_BASE')]),
        ('a/b/b.go', [('NOT COVERED', 'ARITHMETIC_BASE')]),
    ])
    mid = result('github.com/cplieger/thing/a/v2', [
        ('a.go', [('KILLED', 'ARITHMETIC_BASE')]),
        ('b/b.go', [('NOT COVERED', 'ARITHMETIC_BASE')]),
    ])
    leaf = result('github.com/cplieger/thing/a/b/v2', [
        ('b.go', [('KILLED', 'ARITHMETIC_BASE')]),
    ])
    proc, merged = run_merge(tmp, [('.', root), ('a', mid), ('a/b', leaf)], out_name='deep.json')
    check('nested-in-nested: merge succeeds', ok=proc.returncode == 0, detail=proc.stderr)
    if merged is None:
        return
    names = sorted(f['file_name'] for f in merged['files'])
    check('nested-in-nested: every file counted once',
          ok=names == ['a/a.go', 'a/b/b.go', 'thing.go'], detail=str(names))
    check('nested-in-nested: all three killed, none uncovered',
          ok=(merged['mutants_killed'], merged['mutants_not_covered']) == (3, 0),
          detail=f'{merged["mutants_killed"]}/{merged["mutants_not_covered"]}')


def test_root_module_required(tmp: Path) -> None:
    nested = result('github.com/cplieger/envx/yamlenv/v2', [
        ('yamlenv.go', [('KILLED', 'ARITHMETIC_BASE')]),
    ])
    proc, _ = run_merge(tmp, [('yamlenv', nested)], out_name='norootmod.json')
    check('root module required: exits non-zero', ok=proc.returncode != 0)


# ---------------------------------------------------------------------------
# gremlins-scope.py and gremlins-prepare.sh, which calls it
# ---------------------------------------------------------------------------
def exclude_patterns(config: str) -> list[str]:
    """The quoted `unleash.exclude-files` entries of a gremlins config."""
    m = re.search(r'^  exclude-files:[ \t]*\n((?:(?:    .*|[ \t]*)\n)*)', config, re.MULTILINE)
    return re.findall(r"^    - '([^']*)'", m.group(1), re.MULTILINE) if m else []


def gremlins_scope(module_root: Path) -> list[str]:
    """The files gremlins v0.6.0 mutates when run in module_root under its .gremlins.yaml.

    Mirrors internal/engine/engine.go Run: every non-test .go file under the
    module root, minus those whose module-relative path an exclude-files regexp
    matches (internal/exclusion/rules.go uses regexp.MatchString, unanchored).
    """
    patterns = [re.compile(p) for p in exclude_patterns((module_root / '.gremlins.yaml').read_text())]
    out = []
    for p in sorted(module_root.rglob('*.go')):
        rel = p.relative_to(module_root).as_posix()
        if rel.startswith('.git/') or rel.endswith('_test.go'):
            continue
        if not any(r.search(rel) for r in patterns):
            out.append(rel)
    return out


def animap_fixture(repo: Path) -> None:
    """A root module importing an in-tree nested module through a replace (animap's shape)."""
    files = {
        'go.mod': (
            'module github.com/cplieger/animap\n\ngo 1.26\n\n'
            'require github.com/cplieger/animap/mapread v0.0.0\n\n'
            'replace github.com/cplieger/animap/mapread => ./mapread\n'
        ),
        'animap.go': 'package animap\n',
        'animap_test.go': 'package animap\n',
        'cmd/animap/main.go': 'package main\n',
        'cmd/animap/verify.go': 'package main\n\nimport _ "github.com/cplieger/animap/mapread"\n',
        'mapreader/reader.go': 'package mapreader\n',
        'mapread/go.mod': 'module github.com/cplieger/animap/mapread\n\ngo 1.26\n',
        'mapread/encode.go': 'package mapread\n',
        'mapread/decode.go': 'package mapread\n',
        'mapread/encode_test.go': 'package mapread\n',
        'static/ui/go.mod': 'module github.com/cplieger/animap/static/ui\n\ngo 1.26\n',
        'static/ui/ui.go': 'package ui\n',
    }
    commit_fixture(repo, files)


def commit_fixture(repo: Path, files: dict[str, str], message: str = 'fixture') -> str:
    """Write files into the git repo at repo (created when missing), commit them, return the SHA."""
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t']
    if not (repo / '.git').exists():
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run([*git, 'add', '--', *files], check=True)
    subprocess.run([*git, 'commit', '-qm', message], check=True)
    return subprocess.run([*git, 'rev-parse', 'HEAD'], capture_output=True, text=True,
                          check=True).stdout.strip()


def run_scope(target: Path, *modules: str, config: Path = CENTRAL,
              excludes: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCOPE), '--config', str(config), '--target', str(target),
         *(f'--exclude-dir={d}' for d in excludes), '--', *modules],
        capture_output=True, text=True, check=False,
    )


def prepare(tmp: Path, repo: str, *args: str,
            extra_env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess, dict]:
    """Run gremlins-prepare.sh from a ci-checkout-shaped tmp, cloning tmp/src/<repo>.git.

    git's url.insteadOf sends the script's own https://github.com/cplieger/ URL
    to the local bare repo, so the script runs exactly as the jobs call it.
    Returns the process and the GITHUB_OUTPUT it wrote.
    """
    for name, dest in (('scripts', HERE), ('configs', CENTRAL.parent)):
        if not (tmp / name).exists():
            (tmp / name).symlink_to(dest)
    (tmp / 'gitconfig').write_text(f'[url "file://{tmp}/src/"]\n\tinsteadOf = https://github.com/cplieger/\n')
    out = tmp / 'github-output'
    out.write_text('')
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': str(tmp / 'gitconfig'), 'GIT_CONFIG_NOSYSTEM': '1',
           'GITHUB_OUTPUT': str(out), **(extra_env or {})}
    proc = subprocess.run(['bash', str(PREPARE), repo, *args], cwd=tmp, env=env,
                          capture_output=True, text=True, check=False)
    return proc, dict(line.split('=', 1) for line in out.read_text().splitlines())


def publish_fixture(tmp: Path, repo: str, work: Path) -> None:
    """Make the fixture repo at work the bare tmp/src/<repo>.git that prepare clones."""
    (tmp / 'src').mkdir(exist_ok=True)
    subprocess.run(['git', 'clone', '-q', '--bare', str(work), str(tmp / 'src' / f'{repo}.git')],
                   check=True)


def test_prepare_scopes_root_run_off_nested_module(tmp: Path) -> None:
    """The animap failure, through gremlins-prepare.sh's own discovery and config sync.

    The root tests compile mapread/*.go through the replace, so a root run that
    mutates them records real verdicts the merge must refuse. The root's
    gremlins scope has to stop at the nested module's directory, and at the
    static/ui module discovery prunes and so never measures.
    """
    check('prepare: scripts/gremlins-prepare.sh exists', ok=PREPARE.exists())
    (tmp / 'work').mkdir()
    animap_fixture(tmp / 'work')
    publish_fixture(tmp, 'animap', tmp / 'work')
    proc, outputs = prepare(tmp, 'animap')
    head = subprocess.run(['git', '-C', str(tmp / 'work'), 'rev-parse', 'HEAD'],
                          capture_output=True, text=True, check=True).stdout.strip()
    check('prepare scope: measures the root and mapread, not the pruned module, at the head',
          ok=proc.returncode == 0 and outputs == {'sha': head, 'modules': '. mapread'},
          detail=f'rc={proc.returncode} {outputs!r} {proc.stderr[-300:]}')
    if proc.returncode != 0:
        return
    git_config = (tmp / 'target' / '.git' / 'config').read_text()
    check('prepare scope: no auth header is written into the clone',
          ok='extraheader' not in git_config)
    root_scope = gremlins_scope(tmp / 'target')
    check('prepare scope: the root run mutates no file of the nested module',
          ok=root_scope == ['animap.go', 'cmd/animap/verify.go', 'mapreader/reader.go'],
          detail=str(root_scope))
    check('prepare scope: the pruned module gets no config of its own',
          ok=not (tmp / 'target' / 'static' / 'ui' / '.gremlins.yaml').exists())
    nested_scope = gremlins_scope(tmp / 'target' / 'mapread')
    check('prepare scope: the nested run still mutates its own files',
          ok=nested_scope == ['decode.go', 'encode.go'], detail=str(nested_scope))
    check('prepare scope: a leaf module gets the central config unchanged',
          ok=(tmp / 'target' / 'mapread' / '.gremlins.yaml').read_text() == CENTRAL.read_text())


def test_scope_excludes_every_deeper_module(tmp: Path) -> None:
    """Containment at every level, with '.' escaped so a dotted dir matches only itself."""
    for d in ('a.b/c', 'tools'):
        (tmp / d).mkdir(parents=True)
    proc = run_scope(tmp, '.', 'a.b', 'a.b/c', 'tools')
    check('scope nesting: succeeds', ok=proc.returncode == 0, detail=proc.stderr[-300:])
    if proc.returncode != 0:
        return
    central = exclude_patterns(CENTRAL.read_text())
    added = {d: [p for p in exclude_patterns((tmp / d / '.gremlins.yaml').read_text())
                 if p not in central] for d in ('.', 'a.b', 'a.b/c', 'tools')}
    check('scope nesting: each copy adds exactly its deeper modules',
          ok=added == {'.': [r'^a\.b/', r'^a\.b/c/', '^tools/'], 'a.b': ['^c/'],
                       'a.b/c': [], 'tools': []}, detail=str(added))
    root = [re.compile(p) for p in added['.']]
    check('scope nesting: a dotted dir does not swallow its look-alikes',
          ok=not any(r.search(f) for r in root for f in ('aXb/x.go', 'x/a.b/y.go', 'toolsx/z.go')))
    check('scope nesting: the central entries all survive',
          ok=all(p in exclude_patterns((tmp / '.gremlins.yaml').read_text()) for p in central))


def test_scope_excludes_an_unmeasured_module(tmp: Path) -> None:
    """An --exclude-dir module is cut from every outer module and gets no copy."""
    (tmp / 'x' / 'static' / 'y').mkdir(parents=True)
    proc = run_scope(tmp, '.', 'x', excludes=('x/static/y',))
    check('scope unmeasured: succeeds', ok=proc.returncode == 0, detail=proc.stderr[-300:])
    if proc.returncode != 0:
        return
    central = exclude_patterns(CENTRAL.read_text())
    added = {d: [p for p in exclude_patterns((tmp / d / '.gremlins.yaml').read_text())
                 if p not in central] for d in ('.', 'x')}
    check('scope unmeasured: each outer copy excludes it',
          ok=added == {'.': ['^x/', '^x/static/y/'], 'x': ['^static/y/']}, detail=str(added))
    check('scope unmeasured: it gets no config of its own',
          ok=not (tmp / 'x' / 'static' / 'y' / '.gremlins.yaml').exists())


def test_scope_takes_a_dash_dir_as_a_module(tmp: Path) -> None:
    """Discover's charset admits a leading '-'; it must reach the script as a dir, not an option."""
    (tmp / '-h').mkdir()
    proc = run_scope(tmp, '.', '-h')
    written = proc.returncode == 0 and (tmp / '-h' / '.gremlins.yaml').is_file()
    check('scope dash dir: succeeds and writes -h/.gremlins.yaml', ok=written,
          detail=f'rc={proc.returncode} {proc.stderr[-300:]}')
    if not written:
        return
    check('scope dash dir: the root copy excludes it',
          ok='^-h/' in exclude_patterns((tmp / '.gremlins.yaml').read_text()))
    work = tmp / 'work'
    commit_fixture(work, {'go.mod': 'module example.com/dash\n', 'dash.go': 'package dash\n',
                          '-h/go.mod': 'module example.com/dash/h\n', '-h/h.go': 'package h\n'})
    publish_fixture(tmp, 'dash', work)
    proc, outputs = prepare(tmp, 'dash')
    check('scope dash dir: prepare discovers it and scopes it as a dir',
          ok=proc.returncode == 0 and outputs.get('modules') == '. -h'
          and (tmp / 'target' / '-h' / '.gremlins.yaml').is_file()
          and '^-h/' in exclude_patterns((tmp / 'target' / '.gremlins.yaml').read_text()),
          detail=f'rc={proc.returncode} {outputs!r} {proc.stderr[-300:]}')


def test_prepare_pins_a_shard_to_its_plan(tmp: Path) -> None:
    """A shard clones the commit its plan sized and mutates its packages; the plan job the head."""
    work = tmp / 'work'
    sized = commit_fixture(work, {'go.mod': 'module example.com/pin\n', 'pin.go': 'package pin\n',
                                  'a/a.go': 'package a\n', 'b/b.go': 'package b\n'})
    head = commit_fixture(work, {'c/c.go': 'package c\n'}, message='after the plan')
    publish_fixture(tmp, 'pin', work)
    plan = tmp / 'plan.json'
    plan.write_text(json.dumps({'repo': 'pin', 'sha': sized, 'shards': 2, 'modules': {'.': {
        'mode': 'per-package', 'workers': 4, 'shards': [1, 2],
        'packages': {'.': {'shard': 1}, 'a': {'shard': 1}, 'b': {'shard': 2}}}}}))
    proc, outputs = prepare(tmp, 'pin', str(plan), '2')
    check('prepare pin: a shard checks out the commit its plan sized',
          ok=proc.returncode == 0 and outputs.get('sha') == sized
          and not (tmp / 'target' / 'c').exists(),
          detail=f'rc={proc.returncode} {outputs!r} {proc.stderr[-300:]}')
    if proc.returncode == 0:
        check('prepare pin: the shard mutates only the packages its plan gives it',
              ok=gremlins_scope(tmp / 'target') == ['b/b.go'],
              detail=str(gremlins_scope(tmp / 'target')))
    shutil.rmtree(tmp / 'target', ignore_errors=True)
    proc, outputs = prepare(tmp, 'pin')
    check('prepare pin: the plan job measures the head, whole',
          ok=proc.returncode == 0 and outputs.get('sha') == head
          and gremlins_scope(tmp / 'target') == ['a/a.go', 'b/b.go', 'c/c.go', 'pin.go'],
          detail=f'rc={proc.returncode} {outputs!r} {proc.stderr[-300:]}')
    shutil.rmtree(tmp / 'target', ignore_errors=True)
    proc, _ = prepare(tmp, 'pin', str(plan))
    check('prepare pin: a plan without its shard fails before cloning',
          ok=proc.returncode != 0 and not (tmp / 'target').exists(), detail=proc.stderr[-300:])


def test_prepare_clones_anonymously(tmp: Path) -> None:
    """The clone and the plan fetch carry no auth header, even with GH_TOKEN set."""
    work = tmp / 'work'
    sized = commit_fixture(work, {'go.mod': 'module example.com/anon\n', 'anon.go': 'package anon\n'})
    publish_fixture(tmp, 'anon', work)
    plan = tmp / 'plan.json'
    plan.write_text(json.dumps({'repo': 'anon', 'sha': sized, 'shards': 1, 'modules': {'.': {
        'mode': 'per-package', 'workers': 4, 'shards': [1], 'packages': {'.': {'shard': 1}}}}}))
    log = tmp / 'seen.log'
    bindir = tmp / 'bin'
    bindir.mkdir()
    (bindir / 'git').write_text(
        '#!/usr/bin/env bash\n'
        'h=no; case "$*" in *extraheader*|*AUTHORIZATION*) h=yes;; esac\n'
        'sub=; for a in "$@"; do case "$a" in clone|fetch|checkout|rev-parse|ls-files) sub=$a; break;; esac; done\n'
        f'printf "git %s header=%s\\n" "$sub" "$h" >>{str(log)!r}\n'
        f'exec {shutil.which("git")!r} "$@"\n')
    (bindir / 'git').chmod(0o755)
    proc, outputs = prepare(tmp, 'anon', str(plan), '1', extra_env={
        'PATH': f'{bindir}:{os.environ["PATH"]}', 'GH_TOKEN': 'stand-in-credential'})
    seen = log.read_text().splitlines() if log.exists() else []
    check('prepare anonymous: the clone and the plan fetch run without a header',
          ok=proc.returncode == 0 and outputs.get('sha') == sized
          and 'git clone header=no' in seen and 'git fetch header=no' in seen
          and not any('header=yes' in line for line in seen),
          detail=f'rc={proc.returncode} {seen} {proc.stderr[-300:]}')
    git_config = (tmp / 'target' / '.git' / 'config').read_text() if proc.returncode == 0 else ''
    check('prepare anonymous: the clone records no credential',
          ok=proc.returncode == 0 and 'stand-in-credential' not in git_config)


# ---------------------------------------------------------------------------
# configs/gremlins.yaml: only keys gremlins reads
# ---------------------------------------------------------------------------
# The keys gremlins v0.6.0 reads (internal/configuration/configuration.go and
# MutantTypeEnabledKey). Its published JSON Schema cannot gate this: it sets no
# additionalProperties, and it omits exclude-files, workers and invert-logical.
GREMLINS_LAYOUT = {
    'silent': None,
    'unleash': {
        'dry-run': None, 'output-statuses': None, 'output': None, 'tags': None,
        'coverpkg': None, 'workers': None, 'test-cpu': None, 'timeout-coefficient': None,
        'integration': None, 'exclude-files': None, 'diff': None,
        'threshold': {'efficacy': None, 'mutant-coverage': None},
    },
    'mutants': {name: {'enabled': None} for name in gremlins_aggregate.GREMLINS_MUTATORS},
}


def unread_keys(config: object, layout: dict, prefix: str = '') -> list[str]:
    """Dotted paths in config that gremlins never reads under layout."""
    if not isinstance(config, dict):
        return []
    out = []
    for key, value in config.items():
        path = f'{prefix}{key}'
        if key not in layout:
            out.append(path)
        elif isinstance(layout[key], dict):
            out += unread_keys(value, layout[key], f'{path}.')
    return out


def test_central_config_keys_are_read(tmp: Path) -> None:
    pinned = re.search(r'^\s*GREMLINS_VERSION: (\S+)$', WORKFLOW.read_text(), re.MULTILINE)
    check('central config: the key table matches the pinned gremlins',
          ok=bool(pinned) and pinned.group(1) == 'v0.6.0',
          detail=f'workflow pins {pinned and pinned.group(1)}: re-check GREMLINS_LAYOUT and '
                 'gremlins-aggregate.GREMLINS_MUTATORS against that release, then update the pin here')
    config = gremlins_aggregate.load_config(CENTRAL.read_text())
    unread = unread_keys(config, GREMLINS_LAYOUT)
    check('central config: every key is one gremlins reads', ok=not unread, detail=str(unread))
    entries = (config.get('mutants') or {}).values()
    check('central config: every mutants entry sets a bool enabled',
          ok=all(isinstance(e, dict) and isinstance(e.get('enabled'), bool) for e in entries))
    enabled = gremlins_aggregate.enabled_mutators(config)
    check('central config: gremlins runs every mutator type',
          ok=enabled == set(gremlins_aggregate.GREMLINS_MUTATORS),
          detail=f'off: {sorted(set(gremlins_aggregate.GREMLINS_MUTATORS) - enabled)}')
    nested = gremlins_aggregate.load_config(
        'unleash:\n  mutants:\n    invert-logical:\n      enabled: true\n')
    check('central config: a mutants block nested under unleash is caught',
          ok=unread_keys(nested, GREMLINS_LAYOUT) == ['unleash.mutants']
          and gremlins_aggregate.enabled_mutators(nested) == gremlins_aggregate.DEFAULT_MUTATORS)


def test_scope_refuses_what_it_cannot_scope(tmp: Path) -> None:
    (tmp / 'sub').mkdir()
    bare = tmp / 'bare.yaml'
    bare.write_text('unleash:\n  threshold:\n    efficacy: 0\n')
    proc = run_scope(tmp, '.', 'sub', config=bare)
    check('scope refusal: a config without one exclude-files anchor exits non-zero',
          ok=proc.returncode != 0 and 'exclude-files' in proc.stderr, detail=proc.stderr[-300:])
    proc = run_scope(tmp, '.', 'a b')
    check('scope refusal: a dir outside the safe charset exits non-zero',
          ok=proc.returncode != 0 and 'characters' in proc.stderr, detail=proc.stderr[-300:])
    proc = run_scope(tmp, '.', excludes=('a b',))
    check('scope refusal: an --exclude-dir outside the safe charset exits non-zero',
          ok=proc.returncode != 0 and 'characters' in proc.stderr, detail=proc.stderr[-300:])


# ---------------------------------------------------------------------------
# gremlins-aggregate.py — the merged document must still parse downstream
# ---------------------------------------------------------------------------
def test_aggregate_consumes_merged_output(tmp: Path) -> None:
    """A merged document must drive the body, the rolling table and the badge.

    The whole reason the merge happens in the run job (rather than the aggregate
    script growing a per-module mode) is that a repo keeps ONE artifact per
    attempt and ONE published number. This proves the chain holds.
    """
    art = tmp / 'artifacts'
    for attempt in (1, 2, 3):
        d = attempt_dir(art, 'envx', attempt)
        root = result('github.com/cplieger/envx/v2', [
            ('envx.go', [('KILLED', 'ARITHMETIC_BASE'), ('KILLED', 'CONDITIONALS_BOUNDARY'),
                         ('LIVED', 'CONDITIONALS_NEGATION')]),
            ('yamlenv/yamlenv.go', [('NOT COVERED', 'ARITHMETIC_BASE')]),
        ])
        nested = result('github.com/cplieger/envx/yamlenv/v2', [
            ('yamlenv.go', [('KILLED', 'ARITHMETIC_BASE')]),
        ])
        proc, merged = run_merge(tmp, [('.', root), ('yamlenv', nested)],
                                 out_name=f'attempt-{attempt}.json')
        if merged is None:
            check(f'aggregate: attempt {attempt} merged', ok=False, detail=proc.stderr)
            return
        (d / 'gremlins-out.json').write_text(json.dumps(merged))

    badge = tmp / 'badge.json'
    attempts_marker = tmp / 'attempts.txt'
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'envx', '--artifacts-dir', str(art),
         '--week', '2026-08-23 22:00', '--run-url', 'https://example.invalid/run/1',
         '--badge-file', str(badge), '--attempts-marker-file', str(attempts_marker)],
        capture_output=True, text=True, check=False,
    )
    check('aggregate: exits 0 on merged input', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    if proc.returncode != 0:
        return
    body = proc.stdout
    check('aggregate: read all three attempts', ok=attempts_marker.read_text() == '3',
          detail=attempts_marker.read_text())
    check('aggregate: efficacy is the merged 75%', ok='75.0% efficacy' in body,
          detail=body.splitlines()[4] if len(body.splitlines()) > 4 else body[:200])
    check('aggregate: coverage is 100% (nested module measured)',
          ok='100.0% mutant coverage' in body)
    check('aggregate: nested path renders repo-relative in the live-mutant list',
          ok='`envx.go`' in body)
    check('aggregate: badge written', ok=badge.exists() and 'mutation' in badge.read_text(),
          detail=badge.read_text() if badge.exists() else 'missing')


def test_aggregate_flags_disagreeing_verdicts(tmp: Path) -> None:
    """docker-rsync-scheduler's signature: three attempts, three different survivors.

    All three of its live mutants are equivalent, so none can be killed; each
    attempt nonetheless reported a different single survivor and called the other
    two killed. The mean is then noise, and a 100% week is a measurement failure
    rather than a perfect score, so the body must say so.
    """
    art = tmp / 'artifacts-flaky'
    survivors = ['a.go', 'b.go', 'c.go']
    for attempt, lived_in in enumerate(survivors, start=1):
        d = attempt_dir(art, 'rsync', attempt)
        files = [
            (f, [('LIVED' if f == lived_in else 'KILLED', 'ARITHMETIC_BASE')])
            for f in survivors
        ]
        (d / 'gremlins-out.json').write_text(json.dumps(result('github.com/cplieger/x/v2', files)))
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'rsync', '--artifacts-dir', str(art),
         '--week', '2026-08-23 22:00', '--run-url', 'https://example.invalid/run/1'],
        capture_output=True, text=True, check=False,
    )
    check('disagreeing verdicts: exits 0', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    check('disagreeing verdicts: body carries the caution',
          ok='Verdicts disagree' in proc.stdout, detail=proc.stdout[:600])
    check('disagreeing verdicts: caution names the worker-interference cause',
          ok='workers' in proc.stdout.lower())


def test_aggregate_flags_sudden_perfect_week(tmp: Path) -> None:
    """A jump from live mutants to a flawless 100% with no test change."""
    art = tmp / 'artifacts-perfect'
    for attempt in (1, 2, 3):
        d = attempt_dir(art, 'rsync', attempt)
        (d / 'gremlins-out.json').write_text(json.dumps(result('github.com/cplieger/x/v2', [
            ('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('KILLED', 'CONDITIONALS_BOUNDARY')]),
        ])))
    existing = tmp / 'existing.md'
    existing.write_text(
        '# Gremlins mutation testing tracker\n\n'
        '## Rolling 12-week history\n'
        '<!-- gremlins-data -->\n'
        '| Run (UTC) | Mean efficacy | Stddev | Mutant coverage | Live mutants | Δ efficacy |\n'
        '|---|---|---|---|---|---|\n'
        '| 2026-08-16 22:00 | 40.0% | ±0.0% | 100.0% | 3 | -0.0% |\n'
        '<!-- /gremlins-data -->\n'
    )
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'rsync', '--artifacts-dir', str(art),
         '--week', '2026-08-23 22:00', '--run-url', 'https://example.invalid/run/1',
         '--existing-body-file', str(existing), *default_mutators_config(tmp)],
        capture_output=True, text=True, check=False,
    )
    check('sudden perfect week: exits 0', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    check('sudden perfect week: body carries the caution',
          ok='A jump to 100% efficacy' in proc.stdout, detail=proc.stdout[:600])
    check('sudden perfect week: history row preserved', ok='2026-08-16 22:00' in proc.stdout)


def test_aggregate_never_names_an_older_week_as_last_week(tmp: Path) -> None:
    """An unreadable newest row must silence the caution, not promote an older row.

    The newest row here is the legacy fused-delta shape this repo wrote before
    2026-06-22 (da611ef): five columns, with the efficacy delta inside the
    live-mutants cell. Reading the live counts into a list and taking [0] would
    skip that row and print the week BELOW it as "last week", so the caution
    would name 7 live mutants for a week that reported none of them.
    """
    art = tmp / 'artifacts-legacy-row'
    for attempt in (1, 2, 3):
        d = attempt_dir(art, 'rsync', attempt)
        (d / 'gremlins-out.json').write_text(json.dumps(result('github.com/cplieger/x/v2', [
            ('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('KILLED', 'CONDITIONALS_BOUNDARY')]),
        ])))
    existing = tmp / 'existing.md'
    existing.write_text(
        '# Gremlins mutation testing tracker\n\n'
        '## Rolling 12-week history\n'
        '<!-- gremlins-data -->\n'
        '| Run (UTC) | Mean efficacy | Stddev | Mutant coverage | Live mutants | Δ efficacy |\n'
        '|---|---|---|---|---|---|\n'
        '| 2026-08-16 22:00 | 40.0% | ±0.0% | 100.0% | 3 +28.5% |\n'
        '| 2026-08-09 22:00 | 11.5% | ±0.0% | 100.0% | 7 | -0.0% |\n'
        '<!-- /gremlins-data -->\n'
    )
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'rsync', '--artifacts-dir', str(art),
         '--week', '2026-08-23 22:00', '--run-url', 'https://example.invalid/run/1',
         '--existing-body-file', str(existing), *default_mutators_config(tmp)],
        capture_output=True, text=True, check=False,
    )
    check('unreadable newest row: exits 0', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    check('unreadable newest row: caution does not name the older week',
          ok='7 live mutant' not in proc.stdout, detail=proc.stdout[:800])
    check('unreadable newest row: jump caution stays silent',
          ok='jump to 100%' not in proc.stdout.lower(), detail=proc.stdout[:800])
    check('unreadable newest row: both history rows preserved',
          ok='2026-08-16 22:00' in proc.stdout and '2026-08-09 22:00' in proc.stdout)


def test_aggregate_reads_last_week_from_the_newest_row(tmp: Path) -> None:
    """With a readable newest row, the caution quotes THAT row's count."""
    art = tmp / 'artifacts-newest-row'
    for attempt in (1, 2, 3):
        d = attempt_dir(art, 'rsync', attempt)
        (d / 'gremlins-out.json').write_text(json.dumps(result('github.com/cplieger/x/v2', [
            ('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('KILLED', 'CONDITIONALS_BOUNDARY')]),
        ])))
    existing = tmp / 'existing.md'
    existing.write_text(
        '# Gremlins mutation testing tracker\n\n'
        '## Rolling 12-week history\n'
        '<!-- gremlins-data -->\n'
        '| Run (UTC) | Mean efficacy | Stddev | Mutant coverage | Live mutants | Δ efficacy |\n'
        '|---|---|---|---|---|---|\n'
        '| 2026-08-16 22:00 | 40.0% | ±0.0% | 100.0% | 3 | +28.5% |\n'
        '| 2026-08-09 22:00 | 11.5% | ±0.0% | 100.0% | 7 | -0.0% |\n'
        '<!-- /gremlins-data -->\n'
    )
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'rsync', '--artifacts-dir', str(art),
         '--week', '2026-08-23 22:00', '--run-url', 'https://example.invalid/run/1',
         '--existing-body-file', str(existing), *default_mutators_config(tmp)],
        capture_output=True, text=True, check=False,
    )
    check('newest row read: exits 0', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    check('newest row read: caution quotes the newest row count',
          ok='3 live mutant' in proc.stdout, detail=proc.stdout[:800])
    check('newest row read: caution does not quote the older row count',
          ok='7 live mutant' not in proc.stdout, detail=proc.stdout[:800])


def test_aggregate_quiet_on_a_stable_week(tmp: Path) -> None:
    """No caution when every attempt agrees and the score is not a jump to 100%."""
    art = tmp / 'artifacts-stable'
    for attempt in (1, 2, 3):
        d = attempt_dir(art, 'stable', attempt)
        (d / 'gremlins-out.json').write_text(json.dumps(result('github.com/cplieger/x/v2', [
            ('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('LIVED', 'CONDITIONALS_BOUNDARY')]),
        ])))
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'stable', '--artifacts-dir', str(art),
         '--week', '2026-08-23 22:00', '--run-url', 'https://example.invalid/run/1'],
        capture_output=True, text=True, check=False,
    )
    check('stable week: exits 0', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    check('stable week: no caution line',
          ok='Verdicts disagree' not in proc.stdout and 'measurement failure' not in proc.stdout,
          detail=proc.stdout[:400])


def test_aggregate_rerun_replaces_its_own_row(tmp: Path) -> None:
    """One run contributes ONE row, even when its aggregate runs twice.

    This job is a dependent of `run` with `if: always()`, so re-running a failed
    matrix entry re-runs the aggregate too. Both attempts aggregate the SAME
    artifacts for every entry that did not re-run, so a second row is one
    measurement counted twice in the rolling mean. Measured before the fix: run
    28756935137 wrote 2026-07-06 01:31 and 2026-07-07 23:28 into 24 trackers,
    and stryker run 32570714460 did the same to 9.
    """
    art = tmp / 'artifacts-rerun'
    d = attempt_dir(art, 'rerun', 1)
    (d / 'gremlins-out.json').write_text(json.dumps(result('github.com/cplieger/x/v2', [
        ('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('LIVED', 'CONDITIONALS_BOUNDARY')]),
    ])))

    def run(week: str, run_id: str, existing: Path | None) -> str:
        args = [sys.executable, str(AGGREGATE), '--repo', 'rerun',
                '--artifacts-dir', str(art), '--week', week,
                '--run-url', f'https://github.com/cplieger/ci/actions/runs/{run_id}']
        if existing:
            args += ['--existing-body-file', str(existing)]
        p = subprocess.run(args, capture_output=True, text=True, check=False)
        check(f'rerun fixture: aggregate exits 0 ({week})', ok=p.returncode == 0,
              detail=p.stderr[-400:])
        return p.stdout

    def rows(body: str) -> list[str]:
        m = re.search(r'<!-- gremlins-data -->(.*?)<!-- /gremlins-data -->', body, re.DOTALL)
        return [ln for ln in (m.group(1).splitlines() if m else []) if re.match(r'^\| 20', ln)]

    first = tmp / 'b1.md'
    first.write_text(run('2026-07-06 01:31', '28756935137', None))
    check('rerun: attempt 1 writes one row', ok=len(rows(first.read_text())) == 1,
          detail=str(rows(first.read_text())))

    second = tmp / 'b2.md'
    second.write_text(run('2026-07-07 23:28', '28756935137', first))
    got = rows(second.read_text())
    check('rerun: same run REPLACES its row rather than adding one', ok=len(got) == 1,
          detail=str(got))
    check('rerun: the retained row carries the later timestamp',
          ok=bool(got) and '2026-07-07 23:28' in got[0], detail=str(got))

    third = tmp / 'b3.md'
    third.write_text(run('2026-08-21 15:35', '32460445600', second))
    got3 = rows(third.read_text())
    check('rerun: a DIFFERENT run appends, so real history still accumulates',
          ok=len(got3) == 2, detail=str(got3))


def test_aggregate_reads_a_flat_single_artifact(tmp: Path) -> None:
    """A run whose pattern matched ONE artifact extracts flat, and must still count.

    actions/download-artifact documents that a single matched artifact is
    "extracted directly to the specified path", so no per-artifact directory
    exists. The old glob was `gremlins-{repo}-*/gremlins-out.json` and the old repo
    discovery parsed those directory names, so this layout silently yielded zero
    attempts, published no badge, updated no tracker issue and reported success.
    """
    art = tmp / 'artifacts'
    art.mkdir(parents=True)
    (art / 'meta.json').write_text(json.dumps({'repo': 'solo', 'attempt': '1'}))
    (art / 'gremlins-out.json').write_text(
        json.dumps(
            result(
                'github.com/cplieger/x/v2',
                [('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('LIVED', 'CONDITIONALS_BOUNDARY')])],
            )
        )
    )
    badge = tmp / 'badge.json'
    marker = tmp / 'attempts.txt'
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'solo', '--artifacts-dir', str(art),
         '--week', '2026-09-13 22:00', '--run-url', 'https://example.invalid/run/1',
         '--badge-file', str(badge), '--attempts-marker-file', str(marker)],
        capture_output=True, text=True,
    )
    check('flat layout: aggregate exits 0', ok=proc.returncode == 0, detail=proc.stderr)
    check('flat layout: the one attempt is found', ok='found 1 attempt files' in proc.stderr,
          detail=proc.stderr)
    check('flat layout: the attempts marker is 1',
          ok=marker.is_file() and marker.read_text().strip() == '1')
    check('flat layout: a badge is written', ok=badge.is_file() and badge.stat().st_size > 0)
    check('flat layout: the body carries a row', ok='| 2026-09-13 22:00 |' in proc.stdout)


def test_aggregate_skips_an_attempt_it_cannot_attribute(tmp: Path) -> None:
    """No meta.json means the file cannot be tied to a repo, so it is not guessed at.

    Attributing it by directory name is exactly the dependency that broke, and
    counting it against the requested repo would put another repo's mutants in this
    repo's tracker.
    """
    art = tmp / 'artifacts'
    orphan = art / 'gremlins-solo-1'
    orphan.mkdir(parents=True)
    (orphan / 'gremlins-out.json').write_text(
        json.dumps(result('github.com/cplieger/x/v2', [('a.go', [('KILLED', 'ARITHMETIC_BASE')])]))
    )
    badge = tmp / 'badge.json'
    marker = tmp / 'attempts.txt'
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'solo', '--artifacts-dir', str(art),
         '--week', '2026-09-13 22:00', '--run-url', 'https://example.invalid/run/1',
         '--badge-file', str(badge), '--attempts-marker-file', str(marker)],
        capture_output=True, text=True,
    )
    check('unattributable attempt: aggregate exits 0', ok=proc.returncode == 0, detail=proc.stderr)
    check('unattributable attempt: it is not counted',
          ok='found 0 attempt files' in proc.stderr, detail=proc.stderr)
    check('unattributable attempt: it says which file it skipped',
          ok='no readable meta.json' in proc.stderr, detail=proc.stderr)
    check('unattributable attempt: no badge is written, so a good one survives',
          ok=not badge.is_file() or badge.stat().st_size == 0)


def test_aggregate_ignores_another_repos_attempt(tmp: Path) -> None:
    """Every artifact of the run is downloaded, so the repo filter is load-bearing."""
    art = tmp / 'artifacts'
    mine = attempt_dir(art, 'mine', 1)
    (mine / 'gremlins-out.json').write_text(
        json.dumps(result('github.com/cplieger/x/v2', [('a.go', [('KILLED', 'ARITHMETIC_BASE')])]))
    )
    theirs = attempt_dir(art, 'theirs', 1)
    (theirs / 'gremlins-out.json').write_text(
        json.dumps(result('github.com/cplieger/y/v2', [('b.go', [('LIVED', 'ARITHMETIC_BASE')])]))
    )
    marker = tmp / 'attempts.txt'
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'mine', '--artifacts-dir', str(art),
         '--week', '2026-09-13 22:00', '--run-url', 'https://example.invalid/run/1',
         '--attempts-marker-file', str(marker)],
        capture_output=True, text=True,
    )
    check('repo filter: aggregate exits 0', ok=proc.returncode == 0, detail=proc.stderr)
    check('repo filter: only this repo\'s attempt is counted',
          ok='found 1 attempt files' in proc.stderr, detail=proc.stderr)


def shell_function(name: str) -> str:
    """gremlins-run.sh's own `name` function source, or '' when it has none."""
    m = re.search(rf'^{name}\(\) {{\n.*?^}}\n', RUNNER.read_text(), re.MULTILINE | re.DOTALL)
    return m.group(0) if m else ''


def call_shell(name: str, *args: str, deps: tuple[str, ...] = (), prelude: str = '',
               env: dict[str, str] | None = None) -> tuple[str, subprocess.CompletedProcess]:
    """Run gremlins-run.sh's own `name` (after `prelude` and `deps`) under its shell options.

    Returns the extracted `name` source, empty when it or a dep was not found.
    """
    srcs = [shell_function(fn) for fn in (*deps, name)]
    body = prelude + '\n'.join(srcs)
    proc = subprocess.run(
        ['bash', '--norc', '--noprofile', '-c', f'set -euo pipefail\n{body}\n{name} "$@"',
         name, *args],
        capture_output=True, text=True, check=False, env=env,
    )
    return srcs[-1] if all(srcs) else '', proc


def test_timeout_coefficient_scales_with_workers(tmp: Path) -> None:
    src, _ = call_shell('timeout_coefficient', '1')
    check('coefficient: function extracted from gremlins-run.sh', ok=bool(src))
    if not src:
        return
    for workers, want in (('4', '12'), ('2', '6'), ('1', '3')):
        _, proc = call_shell('timeout_coefficient', workers)
        check(f'coefficient: {workers} worker(s) -> {want}',
              ok=proc.returncode == 0 and proc.stdout.strip() == want,
              detail=f'rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}')


def test_integration_decision(tmp: Path) -> None:
    src, _ = call_shell('integration_wanted', '1', '1', '1', '1')
    check('integration: function extracted from gremlins-run.sh', ok=bool(src))
    if not src:
        return
    _, proc = call_shell('integration_wanted', '60', '20', '4', '350')
    check('integration: under a quarter of the cap -> integration', ok=proc.returncode == 0,
          detail=f'rc={proc.returncode} err={proc.stderr!r}')
    check('integration: prints the estimate and the budget',
          ok=proc.stdout.strip() == 'estimate=300s budget=5250s', detail=repr(proc.stdout))
    _, proc = call_shell('integration_wanted', '100', '419', '4', '350')
    check('integration: over a quarter of the cap -> per-package', ok=proc.returncode == 1,
          detail=f'rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}')
    _, proc = call_shell('integration_wanted', '5250', '4', '4', '350')
    check('integration: exactly a quarter of the cap -> per-package', ok=proc.returncode == 1,
          detail=f'rc={proc.returncode} out={proc.stdout!r}')
    _, proc = call_shell('integration_wanted', '60', '20', '1', '350')
    check('integration: divides by the workers gremlins is given', ok=proc.returncode == 0
          and proc.stdout.strip() == 'estimate=1200s budget=5250s', detail=repr(proc.stdout))
    for label, args in (
        ('suite unmeasured', ('', '20', '4', '350')),
        ('dry run gave no count', ('60', '', '4', '350')),
        ('cap missing', ('60', '20', '4', '')),
        ('non-numeric count', ('60', '2O', '4', '350')),
        ('leading zero, which bash would read as octal', ('60', '08', '4', '350')),
        ('zero workers', ('60', '20', '0', '350')),
        ('wrong arity', ('60', '20', '4')),
    ):
        _, proc = call_shell('integration_wanted', *args)
        check(f'integration: measurement failed ({label}) -> per-package',
              ok=proc.returncode == 1 and proc.stdout == '' and proc.stderr == '',
              detail=f'rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}')


def test_runner_mode_selection(tmp: Path) -> None:
    guards = ''.join(re.findall(r'^: "\$\{.*\n', RUNNER.read_text(), re.MULTILINE))
    check('runner mode: env guards extracted from gremlins-run.sh', ok='OUT:?' in guards,
          detail=repr(guards))
    base = {'PATH': '/usr/bin:/bin', **dict.fromkeys((
        'GREMLINS_VERSION', 'GREMLINS_PATCHES', 'WORKERS_MAX', 'GOMEM_MAX_MB', 'GOMEM_SOLO_MB',
        'PROBE_TIMEOUT', 'OUT'), 'x')}
    src, proc = call_shell('select_mode', '60', '20', '4', deps=('integration_wanted',),
                           prelude=guards, env=base)
    check('runner mode: select_mode extracted from gremlins-run.sh', ok=bool(src))
    check('runner mode: no cap passes the env guards and keeps per-package',
          ok=proc.returncode == 0 and proc.stdout == 'per-package\n',
          detail=f'rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}')
    check('runner mode: no cap is logged as unmeasured',
          ok=proc.stderr == 'mode=per-package (suite=60s mutants=20 workers=4 cap=unmeasured)\n',
          detail=repr(proc.stderr))
    _, proc = call_shell('select_mode', '60', '20', '4', deps=('integration_wanted',),
                         prelude=guards, env={**base, 'JOB_TIMEOUT_MINUTES': ''})
    check('runner mode: an empty cap keeps per-package',
          ok=proc.returncode == 0 and proc.stdout == 'per-package\n',
          detail=f'rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}')
    _, proc = call_shell('select_mode', '60', '20', '4', deps=('integration_wanted',),
                         prelude=guards, env={**base, 'JOB_TIMEOUT_MINUTES': '350'})
    check('runner mode: under the cap -> integration, logged with its inputs',
          ok=proc.returncode == 0 and proc.stdout == 'integration\n' and proc.stderr
          == 'mode=integration (suite=60s mutants=20 workers=4 cap=350min estimate=300s '
          'budget=5250s)\n', detail=f'rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}')
    _, proc = call_shell('select_mode', '', '', '1', deps=('integration_wanted',),
                         prelude=guards, env={**base, 'JOB_TIMEOUT_MINUTES': '350'})
    check('runner mode: unmeasured suite -> per-package, logged as unmeasured',
          ok=proc.returncode == 0 and proc.stdout == 'per-package\n' and proc.stderr
          == 'mode=per-package (suite=unmeasured mutants=unmeasured workers=1 cap=350min)\n',
          detail=f'rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}')


def test_workflow_applies_every_patch(tmp: Path) -> None:
    """A patch in scripts/ that gremlins-container.sh does not mount and list is never applied."""
    step = CONTAINER.read_text() if CONTAINER.exists() else ''
    check('patches: scripts/gremlins-container.sh exists', ok=bool(step))
    on_disk = sorted(p.name for p in HERE.glob('gremlins-*.patch'))
    mounts = dict(re.findall(
        r'-v "\$\{PWD\}/scripts/(gremlins-[^"]+\.patch)":(/[^:"]+):ro', step))
    listed = re.search(r'-e GREMLINS_PATCHES="([^"]*)"', step)
    paths = listed.group(1).split() if listed else []
    check('patches: every scripts/gremlins-*.patch is mounted',
          ok=bool(on_disk) and sorted(mounts) == on_disk, detail=f'{sorted(mounts)} != {on_disk}')
    check('patches: GREMLINS_PATCHES lists every mounted patch once',
          ok=sorted(paths) == sorted(mounts.values()) and len(set(paths)) == len(paths),
          detail=f'{paths} vs {sorted(mounts.values())}')
    runner = RUNNER.read_text()
    check('patches: gremlins-run.sh says when to delete each patch',
          ok=all(name in runner for name in on_disk),
          detail=str([n for n in on_disk if n not in runner]))
    # gremlins' own tests cover the parse and the executor, not cmd/unleash.go;
    # without this line the patch builds, passes and changes no timeout.
    pkgtimeout = (HERE / 'gremlins-pkgtimeout.patch').read_text()
    check('patches: gremlins-pkgtimeout.patch wires the per-package times into the executor',
          ok=bool(re.search(r'^\+.*engine\.WithPackageElapsed\(cProfile\.PackageElapsed\)',
                            pkgtimeout, re.MULTILINE)))


def test_merge_records_the_mode(tmp: Path) -> None:
    root = result('github.com/cplieger/envx/v2', [
        ('envx.go', [('KILLED', 'ARITHMETIC_BASE')]),
        ('yamlenv/yamlenv.go', [('NOT COVERED', 'ARITHMETIC_BASE')]),
    ])
    nested = result('github.com/cplieger/envx/yamlenv/v2', [
        ('yamlenv.go', [('KILLED', 'ARITHMETIC_BASE')]),
    ])
    _, merged = run_merge(tmp, [('.', root)], out_name='m1.json', modes={'.': 'integration\n'})
    check('merge mode: one integration module -> integration',
          ok=merged is not None and merged['mode'] == 'integration',
          detail=str(merged and merged.get('mode')))
    _, merged = run_merge(tmp, [('.', root), ('yamlenv', nested)], out_name='m2.json',
                          modes={'.': 'integration\n', 'yamlenv': 'per-package\n'})
    check('merge mode: modules that differ -> mixed',
          ok=merged is not None and merged['mode'] == 'mixed'
          and [m['mode'] for m in merged['modules']] == ['integration', 'per-package'],
          detail=str(merged and merged.get('modules')))
    for p in tmp.glob('*.mode'):
        p.unlink()
    _, merged = run_merge(tmp, [('.', root)], out_name='m3.json')
    check('merge mode: no mode file -> per-package',
          ok=merged is not None and merged['mode'] == 'per-package',
          detail=str(merged and merged.get('mode')))
    proc, _ = run_merge(tmp, [('.', root)], out_name='m4.json', modes={'.': 'sharded\n'})
    check('merge mode: an unknown mode fails the attempt',
          ok=proc.returncode != 0 and 'sharded' in proc.stderr, detail=proc.stderr[-300:])


MODE_HISTORY = (
    '# Gremlins mutation testing tracker\n\n'
    '## Rolling 12-week history\n'
    '<!-- gremlins-data -->\n'
    '| Run (UTC) | Mean efficacy | Stddev | Mutant coverage | Live mutants | Δ efficacy | Mode '
    '| Mutators |\n'
    '|---|---|---|---|---|---|---|---|\n'
    '| 2026-09-27 22:00 | 90.0% | ±0.0% | 60.0% | 2 | +0.0% | per-package | all | <!-- run:3 -->\n'
    '| 2026-09-20 22:00 | 90.0% | ±0.0% | 60.0% | 2 | — | per-package | all | <!-- run:2 -->\n'
    '<!-- /gremlins-data -->\n'
)


def aggregate_week(tmp: Path, name: str, mode: str, files: list, existing: str,
                   run_id: str, config: tuple[str, ...] = ()) -> tuple[subprocess.CompletedProcess, str]:
    art = tmp / f'art-{name}'
    for attempt in (1, 2, 3):
        doc = result('github.com/cplieger/x/v2', files)
        doc['mode'] = mode
        (attempt_dir(art, 'sw', attempt) / 'gremlins-out.json').write_text(json.dumps(doc))
    body = tmp / f'existing-{name}.md'
    body.write_text(existing)
    marker = tmp / f'regression-{name}.txt'
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'sw', '--artifacts-dir', str(art),
         '--week', f'2026-10-{int(run_id):02d} 22:00', '--run-url',
         f'https://github.com/cplieger/ci/actions/runs/{run_id}',
         '--existing-body-file', str(body), '--regression-marker-file', str(marker), *config],
        capture_output=True, text=True, check=False,
    )
    return proc, marker.read_text() if marker.exists() else ''


def test_aggregate_marks_a_mode_switch(tmp: Path) -> None:
    half = [('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('LIVED', 'CONDITIONALS_BOUNDARY')])]
    proc, flag = aggregate_week(tmp, 'control', 'per-package', half, MODE_HISTORY, '4')
    check('mode switch: control week exits 0', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    check('mode switch: the same drop without a switch raises the label', ok=flag == 'true',
          detail=flag)
    check('mode switch: control row records per-package with no switch mark',
          ok='| per-package | all | <!-- run:4 -->' in proc.stdout, detail=proc.stdout[:900])

    proc, flag = aggregate_week(tmp, 'switch', 'integration', half, MODE_HISTORY, '4')
    check('mode switch: switch week exits 0', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    check('mode switch: the row marks the switch and the expected jump',
          ok='| integration (switched from per-package; score jump expected) | all | <!-- run:4 -->'
          in proc.stdout, detail=proc.stdout[:900])
    check('mode switch: the switch week does not raise the label', ok=flag == 'false', detail=flag)
    check('mode switch: the header says why', ok='Mode switched from per-package to integration'
          in proc.stdout, detail=proc.stdout[:900])

    # Re-aggregating the same run must still see the switch, not its own row.
    rerun, flag = aggregate_week(tmp, 'rerun', 'integration', half, proc.stdout, '4')
    check('mode switch: a re-aggregate of the switch run keeps the mark',
          ok='(switched from per-package; score jump expected) | all | <!-- run:4 -->'
          in rerun.stdout
          and rerun.stdout.count('<!-- run:4 -->') == 1 and flag == 'false',
          detail=rerun.stdout[:900])

    after, flag = aggregate_week(tmp, 'after', 'integration', half, proc.stdout, '5')
    check('mode switch: the next week in the new mode is unmarked',
          ok='| integration | all | <!-- run:5 -->' in after.stdout, detail=after.stdout[:900])
    check('mode switch: the next week compares only integration rows', ok=flag == 'false',
          detail=flag)

    perfect = [('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('KILLED', 'CONDITIONALS_BOUNDARY')])]
    proc, _ = aggregate_week(tmp, 'perfect', 'integration', perfect, MODE_HISTORY, '4')
    check('mode switch: a switch to 100% is not called a measurement failure',
          ok='jump to 100%' not in proc.stdout.lower() and 'Mode switched' in proc.stdout,
          detail=proc.stdout[:900])


# The rows the weekly runner wrote while configs/gremlins.yaml nested its
# mutants block under unleash: no Mutators cell, and gremlins' default types.
LEGACY_HISTORY = (
    '# Gremlins mutation testing tracker\n\n'
    '## Rolling 12-week history\n'
    '<!-- gremlins-data -->\n'
    '| Run (UTC) | Mean efficacy | Stddev | Mutant coverage | Live mutants | Δ efficacy | Mode |\n'
    '|---|---|---|---|---|---|---|\n'
    '| 2026-09-27 22:00 | 90.0% | ±0.0% | 60.0% | 2 | +0.0% | per-package | <!-- run:3 -->\n'
    '| 2026-09-20 22:00 | 90.0% | ±0.0% | 60.0% | 2 | — | per-package | <!-- run:2 -->\n'
    '<!-- /gremlins-data -->\n'
)


def test_aggregate_starts_a_baseline_when_the_mutators_change(tmp: Path) -> None:
    half = [('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('LIVED', 'INVERT_LOGICAL')])]
    legacy = default_mutators_config(tmp)
    proc, flag = aggregate_week(tmp, 'm-control', 'per-package', half, LEGACY_HISTORY, '4', legacy)
    check('mutator switch: control week exits 0', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    check('mutator switch: the same drop on the same mutators raises the label', ok=flag == 'true',
          detail=flag)
    check('mutator switch: control row records the default set unmarked',
          ok='| per-package | default | <!-- run:4 -->' in proc.stdout, detail=proc.stdout[:900])

    proc, flag = aggregate_week(tmp, 'm-switch', 'per-package', half, LEGACY_HISTORY, '4')
    check('mutator switch: the first all-mutators week exits 0', ok=proc.returncode == 0,
          detail=proc.stderr[-400:])
    check('mutator switch: the row marks the switch',
          ok='| per-package | all (switched from default; score shift expected) | <!-- run:4 -->'
          in proc.stdout, detail=proc.stdout[:900])
    check('mutator switch: the first all-mutators week does not raise the label', ok=flag == 'false',
          detail=flag)
    check('mutator switch: the header says why',
          ok='Mutators switched from default to all' in proc.stdout and '**Trend**' not in proc.stdout,
          detail=proc.stdout[:900])

    rerun, flag = aggregate_week(tmp, 'm-rerun', 'per-package', half, proc.stdout, '4')
    check('mutator switch: a re-aggregate of the switch run keeps the mark',
          ok='all (switched from default; score shift expected) | <!-- run:4 -->' in rerun.stdout
          and rerun.stdout.count('<!-- run:4 -->') == 1 and flag == 'false',
          detail=rerun.stdout[:900])

    worse = [('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('LIVED', 'INVERT_LOGICAL'),
                       ('LIVED', 'INVERT_BITWISE'), ('LIVED', 'INVERT_LOOPCTRL')])]
    after, flag = aggregate_week(tmp, 'm-after', 'per-package', worse, proc.stdout, '5')
    check('mutator switch: the next week on all mutators is unmarked',
          ok='| per-package | all | <!-- run:5 -->' in after.stdout, detail=after.stdout[:900])
    check('mutator switch: the next week compares against the new baseline only', ok=flag == 'true'
          and '-25.0% from 12-week mean (50.0%)' in after.stdout, detail=after.stdout[:900])

    perfect = [('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('KILLED', 'INVERT_LOGICAL')])]
    proc, _ = aggregate_week(tmp, 'm-perfect', 'per-package', perfect, LEGACY_HISTORY, '4')
    check('mutator switch: a switch to 100% is not called a measurement failure',
          ok='jump to 100%' not in proc.stdout.lower() and 'Mutators switched' in proc.stdout,
          detail=proc.stdout[:900])


def test_enabled_mutators_follows_gremlins(tmp: Path) -> None:
    """gremlins' configuration.Get[bool] reads a non-bool as false and an absent key as the default."""
    em = gremlins_aggregate.enabled_mutators
    default = gremlins_aggregate.DEFAULT_MUTATORS
    cases = {
        'no mutants key keeps the defaults': ({}, default),
        'an explicit false turns a default type off':
            ({'mutants': {'arithmetic-base': {'enabled': False}}}, default - {'arithmetic-base'}),
        'a string is not true': ({'mutants': {'invert-logical': {'enabled': 'true'}}}, default),
        'a non-bool turns a default type off':
            ({'mutants': {'arithmetic-base': {'enabled': 'yes'}}}, default - {'arithmetic-base'}),
        'a null enabled keeps the default':
            ({'mutants': {'arithmetic-base': {'enabled': None}}}, default),
        'an entry without enabled keeps the default':
            ({'mutants': {'invert-logical': True, 'arithmetic-base': {}}}, default),
        'a top-level entry turns a type on':
            ({'mutants': {'invert-logical': {'enabled': True}}}, default | {'invert-logical'}),
    }
    for name, (config, want) in cases.items():
        check(f'enabled mutators: {name}', ok=em(config) == want, detail=str(sorted(em(config))))
    for word in ('yes', 'on', 'y', 'True'):
        doc = gremlins_aggregate.load_config(f'mutants:\n  invert-logical:\n    enabled: {word}\n')
        value = doc['mutants']['invert-logical']['enabled']
        on = word == 'True'
        check(f'enabled mutators: YAML 1.2 reads enabled: {word} as gremlins does',
              ok=isinstance(value, bool) == on and ('invert-logical' in em(doc)) == on,
              detail=repr(value))
    label = gremlins_aggregate.mutators_label
    check('mutators label: all, default, and a sorted list otherwise',
          ok=label(frozenset(gremlins_aggregate.GREMLINS_MUTATORS)) == 'all'
          and label(default) == 'default'
          and label(frozenset({'invert-logical', 'arithmetic-base'})) == 'arithmetic-base+invert-logical'
          and label(frozenset()) == 'none')
    proc = subprocess.run(
        [sys.executable, str(AGGREGATE), '--repo', 'x', '--artifacts-dir', str(tmp),
         '--week', '2026-10-04 22:00', '--run-url', 'https://example.invalid/run/1',
         '--config', str(tmp / 'missing.yaml')],
        capture_output=True, text=True, check=False,
    )
    check('mutators label: an unreadable --config fails loudly',
          ok=proc.returncode == 1 and 'cannot read' in proc.stderr, detail=proc.stderr[-300:])


# ---------------------------------------------------------------------------
# gremlins-shard.py and the shard wiring: a module whose mutants exceed one job
# ---------------------------------------------------------------------------
def load_script(name: str, path: Path):
    """Import a dash-named script as a module; None when it does not exist yet."""
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# deadset-go at 8ea1867, the tree run 42520923530 cloned. Per package: RUNNABLE
# and NOT COVERED from a per-package dry run under that run's config, and the
# milliseconds gremlins-run.sh's time_package_mutants measured on a 4-CPU slice
# of a shared host (None: no row). The env is that run's attempt 1: the suite
# and dry-run count from its mode line, setup from job start to it.
DEADSET_GO = {
    'module': 'github.com/cplieger/deadset-go/v2',
    'env': {'workers': '4', 'mode': 'per-package', 'suite_secs': '132', 'mutants': '5632',
            'setup_secs': '500'},
    'packages': {
        'cmd/deadset-go': (228, 58, 132755), 'internal/buildconstraint': (48, 20, 737),
        'internal/config': (167, 10, 1576), 'internal/conventions': (52, 50, 547),
        'internal/deps': (57, 1, 2183), 'internal/edges': (10, 1, 894),
        'internal/exempt': (1576, 274, 27768), 'internal/facts': (62, 719, 2384),
        'internal/failure': (10, 5, 416), 'internal/findings': (969, 110, 39016),
        'internal/gotypes': (0, 11, 195), 'internal/inventory': (52, 181, 1304),
        'internal/jsondoc': (5, 0, 532), 'internal/liveness': (391, 76, 19247),
        'internal/load': (586, 129, 10337), 'internal/matrix': (29, 10, 1375),
        'internal/memory': (136, 63, 1224), 'internal/resolve': (0, 65, 693),
        'internal/roots': (43, 199, 1284), 'internal/run': (98, 172, 3452),
        'internal/scope': (40, 2, 972), 'internal/suppress': (47, 3, 3023),
    },
}
# Longest-first over the fixture: 2 shards project 256 and 294 min, 3 project
# 193, 174 and 193. Run 42520923530 spent 163 min in internal/exempt alone.
FIXTURE_SHARDS = 3
# Wall minutes on 4 workers of the slow packages run 42520923530's attempt 1
# finished, from its log. internal/conventions (1.2 min) is left out: 4 hangs at
# the 60 s floor are most of it, and the projection has no term for hangs.
RUN_42520923530_MINUTES = {'internal/exempt': 162.9, 'internal/findings': 96.9,
                           'cmd/deadset-go': 32.0, 'internal/liveness': 23.4}
CI_CAP_MINUTES = 350
SHARD_TARGET_MINUTES = 240
FIXTURE_SHA = '8ea1867888623f3fab42af9c0404571c0ae8e851'


def write_plan_dir(d: Path, module: str, env: dict | None, packages: dict | None) -> None:
    """The files gremlins-run.sh writes under PLAN_DIR, built from a fixture.

    env None: no files (the module's container failed). A None value: no such
    line ('sized': None is a plan that stopped early). packages None: no dry run
    report (gremlins writes none for a module with no mutants).
    """
    d.mkdir(parents=True)
    if env is None:
        return
    lines = {**env, 'sized': env.get('sized', '1')}
    (d / 'plan.env').write_text(''.join(f'{k}={v}\n' for k, v in lines.items() if v is not None))
    if env.get('mode') != 'per-package':
        return
    if packages is None:
        (d / 'mutant-ms.tsv').write_text(f'{module}\t900\n')
        return
    files, times = [], []
    for pkg, (runnable, uncovered, mutant_ms) in packages.items():
        files.append({'file_name': 'x.go' if pkg == '.' else f'{pkg}/x.go', 'mutations': [
            {'type': 'ARITHMETIC_BASE', 'status': status, 'line': i + 1, 'column': 1}
            for i, status in enumerate(['RUNNABLE'] * runnable + ['NOT COVERED'] * uncovered)
        ]})
        if mutant_ms is not None:
            times.append(f'{module if pkg == "." else f"{module}/{pkg}"}\t{mutant_ms}\n')
    (d / 'dry-run.json').write_text(json.dumps({'go_module': module, 'files': files}))
    (d / 'mutant-ms.tsv').write_text(''.join(times))


def run_shard(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SHARD), *args], capture_output=True, text=True,
                          check=False)


def plan_repo(tmp: Path, repo: str, modules: dict[str, tuple[str, dict | None, dict | None]],
              target: int = SHARD_TARGET_MINUTES) -> tuple[subprocess.CompletedProcess, dict | None]:
    """Run `gremlins-shard.py plan` over fixture plan dirs; modules: {dir: (go module, env, packages)}.

    The plan comes back only when the planner exits 0; written_plan reads one it wrote anyway.
    """
    args = []
    for dir_, (module, env, packages) in modules.items():
        d = tmp / f'plan-{repo}' / dir_.replace('/', '_')
        write_plan_dir(d, module, env, packages)
        args += ['--module', f'{dir_}={d}']
    out = tmp / f'plan-{repo}.json'
    proc = run_shard('plan', '--repo', repo, '--sha', FIXTURE_SHA, '--target-minutes', str(target),
                     *args, '--out', str(out))
    return proc, json.loads(out.read_text()) if proc.returncode == 0 and out.exists() else None


def written_plan(tmp: Path, repo: str) -> dict | None:
    out = tmp / f'plan-{repo}.json'
    return json.loads(out.read_text()) if out.exists() else None


def fixture_work_secs(packages: dict) -> dict[str, float]:
    """Each package's projected worker-seconds: RUNNABLE x its mutant time."""
    dearest = max((c for *_, c in packages.values() if c is not None), default=0)
    return {pkg: runnable * (ms if ms is not None else dearest) / 1000
            for pkg, (runnable, _, ms) in packages.items()}


def test_shard_projects_deadset_go_past_one_job(tmp: Path) -> None:
    check('shard: scripts/gremlins-shard.py exists', ok=SHARD.exists())
    env, packages = DEADSET_GO['env'], DEADSET_GO['packages']
    proc, plan = plan_repo(tmp, 'deadset-go', {'.': (DEADSET_GO['module'], env, packages)})
    check('shard projection: plan succeeds', ok=plan is not None,
          detail=f'rc={proc.returncode} {proc.stderr[-400:]}')
    if plan is None:
        return
    work = fixture_work_secs(packages)
    setup = float(env['setup_secs']) + float(env['suite_secs'])
    one_job = (setup + sum(work.values()) / int(env['workers'])) / 60
    check('shard projection: one job is setup + suite + sum(RUNNABLE x mutant time) / workers',
          ok=abs(plan['one_job_minutes'] - one_job) < 0.1,
          detail=f'{plan["one_job_minutes"]} vs {one_job:.1f}')
    check('shard projection: one job would pass the cap run 42520923530 was cancelled at',
          ok=plan['one_job_minutes'] > CI_CAP_MINUTES, detail=str(plan['one_job_minutes']))
    got = plan['modules']['.']['packages']
    with_mutants = {p for p, (r, n, *_) in packages.items() if r + n}
    check('shard projection: every package with a mutant is assigned, none twice',
          ok=set(got) == with_mutants and all(isinstance(v['shard'], int) for v in got.values()),
          detail=str(sorted(set(got) ^ with_mutants)))
    k = plan['shards']
    check(f'shard projection: deadset-go needs {FIXTURE_SHARDS} shards',
          ok=k == FIXTURE_SHARDS, detail=str(k))
    projected = {p: e['work_secs'] / 60 for p, e in got.items()}
    check('shard projection: no package projects under its wall time in run 42520923530',
          ok=all(projected[p] >= observed for p, observed in RUN_42520923530_MINUTES.items()),
          detail=str({p: round(projected[p], 1) for p in RUN_42520923530_MINUTES}))
    check('shard projection: every shard fits the target',
          ok=len(plan['projected_minutes']) == k
          and all(m <= SHARD_TARGET_MINUTES for m in plan['projected_minutes']),
          detail=str(plan['projected_minutes']))
    loads = [0.0] * k
    for pkg, entry in got.items():
        loads[entry['shard'] - 1] += work[pkg] / int(env['workers'])
    check('shard projection: each shard projects its own packages plus one setup',
          ok=all(abs((setup + w) / 60 - m) < 0.1 for w, m in zip(loads, plan['projected_minutes'], strict=False)),
          detail=f'{[round((setup + w) / 60, 1) for w in loads]} vs {plan["projected_minutes"]}')
    check('shard projection: the two slowest packages land in different shards',
          ok=got['internal/exempt']['shard'] != got['internal/findings']['shard'])
    shard = load_script('gremlins_shard', SHARD)
    items = [shard.Item('.', p, w / int(env['workers'])) for p, w in work.items()
             if sum(packages[p][:2])]
    fewer = shard.pack(items, k - 1, {'.': setup})
    check('shard projection: one shard fewer would miss the target',
          ok=max(b.load for b in fewer) / 60 > SHARD_TARGET_MINUTES,
          detail=str([round(b.load / 60, 1) for b in fewer]))


def test_shard_bin_pack_longest_first(tmp: Path) -> None:
    shard = load_script('gremlins_shard', SHARD)
    check('bin pack: gremlins-shard.py importable', ok=shard is not None)
    if shard is None:
        return

    def assignment(bins) -> list[list[str]]:
        return [sorted(it.package for it in b.items) for b in bins]

    items = [shard.Item('.', name, work) for name, work in
             (('p1', 1), ('p7', 7), ('p3', 3), ('p6', 6), ('p2', 2), ('p5', 5), ('p4', 4))]
    bins = shard.pack(items, 2, {'.': 0})
    check('bin pack: longest first into the least loaded shard, ties to the lower index',
          ok=assignment(bins) == [['p3', 'p4', 'p7'], ['p1', 'p2', 'p5', 'p6']]
          and [b.load for b in bins] == [14, 14], detail=str(assignment(bins)))
    check('bin pack: a setup every shard pays does not change the packing',
          ok=assignment(shard.pack(items, 2, {'.': 100})) == assignment(bins))
    check('bin pack: the same input packs the same way', ok=assignment(shard.pack(
        list(reversed(items)), 2, {'.': 0})) == assignment(bins))
    mixed = [shard.Item('x', 'a', 5), shard.Item('x', 'b', 5), shard.Item('y', 'c', 10),
             shard.Item('x', 'z', 0)]
    bins = shard.pack(mixed, 2, {'x': 100, 'y': 0})
    check('bin pack: a second setup keeps a module whole when splitting it buys nothing',
          ok=assignment(bins) == [['c'], ['a', 'b', 'z']], detail=str(assignment(bins)))
    k, bins = shard.choose(items, {'.': 0}, 15)
    check('bin pack: the fewest shards that fit the target', ok=k == 2, detail=str(k))
    k, bins = shard.choose(items, {'.': 0}, 6)
    check('bin pack: a package over the target alone stops at the shard count that isolates it',
          ok=k == 4 and max(b.load for b in bins) == 7, detail=f'{k} {[b.load for b in bins]}')
    k, bins = shard.choose([], {'.': 0}, 6)
    check('bin pack: no items is one shard', ok=k == 1 and len(bins) == 1)


SMALL_PACKAGES = {'.': (40, 3, 9000), 'internal/a': (20, 0, 2500), 'internal/b': (0, 4, None),
                  'internal/c': (7, 1, None)}
SMALL_ENV = {'workers': '4', 'mode': 'per-package', 'suite_secs': '9', 'mutants': '400',
             'setup_secs': '120'}


def test_shard_small_module_stays_one_unchanged_job(tmp: Path) -> None:
    proc, plan = plan_repo(tmp, 'small', {'.': ('github.com/cplieger/small/v2', SMALL_ENV,
                                                SMALL_PACKAGES)})
    check('one shard: plan succeeds', ok=plan is not None, detail=proc.stderr[-400:])
    if plan is None:
        return
    check('one shard: a module under the target gets one shard', ok=plan['shards'] == 1,
          detail=str(plan['shards']))
    check('one shard: a package with no timing row costs as much as the dearest one',
          ok=plan['modules']['.']['packages']['internal/c']['mutant_secs'] == 9.0,
          detail=str(plan['modules']['.']['packages'].get('internal/c')))
    plans = tmp / 'plans'
    plans.mkdir()
    (plans / 'plan.json').write_text(json.dumps(plan))
    proc = run_shard('matrix', '--repos', json.dumps(['small']), '--plans', str(plans),
                     '--attempts', '3', '--artifacts', json.dumps(['shard-plan-small']))
    matrix = json.loads(proc.stdout) if proc.returncode == 0 else None
    check('one shard: its matrix entries are the unsharded ones the fanout always built',
          ok=matrix == [{'repo': 'small', 'attempt': n} for n in (1, 2, 3)],
          detail=f'rc={proc.returncode} {proc.stdout[:300]} {proc.stderr[-300:]}')
    integration = {'workers': '4', 'mode': 'integration', 'suite_secs': '30', 'mutants': '300',
                   'setup_secs': '200'}
    proc, plan = plan_repo(tmp, 'integ', {'.': ('github.com/cplieger/integ/v2', integration, {})})
    check('one shard: an integration module plans from the run inputs alone',
          ok=plan is not None and plan['shards'] == 1
          and abs(plan['one_job_minutes'] - (200 + 30 + 30 * 300 / 4) / 60) < 0.1,
          detail=f'{plan and plan["one_job_minutes"]} {proc.stderr[-300:]}')


def test_shard_never_splits_an_integration_module(tmp: Path) -> None:
    heavy = {'workers': '4', 'mode': 'integration', 'suite_secs': '60', 'mutants': '1200',
             'setup_secs': '300'}
    modules = {'.': ('github.com/cplieger/multi/v2', heavy, {}),
               'tools': ('github.com/cplieger/multi/tools', DEADSET_GO['env'],
                         DEADSET_GO['packages'])}
    proc, plan = plan_repo(tmp, 'multi', modules)
    check('integration item: plan succeeds', ok=plan is not None, detail=proc.stderr[-400:])
    if plan is None:
        return
    root = plan['modules']['.']
    check('integration item: the integration module runs whole in exactly one shard',
          ok=len(root['shards']) == 1 and root['packages'] == {}, detail=str(root))
    tools = plan['modules']['tools']
    used = {e['shard'] for e in tools['packages'].values()}
    check('integration item: the per-package module lists the shards its packages use',
          ok=sorted(used) == tools['shards'], detail=f'{sorted(used)} vs {tools["shards"]}')


def matrix_of(tmp: Path, plan: dict) -> tuple[subprocess.CompletedProcess, list | None]:
    plans = tmp / f'plans-{plan["repo"]}'
    plans.mkdir()
    (plans / 'plan.json').write_text(json.dumps(plan))
    proc = run_shard('matrix', '--repos', json.dumps([plan['repo']]), '--plans', str(plans),
                     '--attempts', '1', '--artifacts', json.dumps([f'shard-plan-{plan["repo"]}']))
    return proc, json.loads(proc.stdout) if proc.returncode == 0 else None


def test_shard_sizes_a_zero_mutant_module_as_zero(tmp: Path) -> None:
    deadset = (DEADSET_GO['module'], DEADSET_GO['env'], DEADSET_GO['packages'])
    _, alone = plan_repo(tmp, 'alone', {'.': deadset})
    proc, plan = plan_repo(tmp, 'zero', {'.': deadset,
                                         'empty': ('github.com/cplieger/zero/empty', SMALL_ENV, None)})
    check('zero mutants: a per-package module with no dry run report plans',
          ok=plan is not None, detail=f'rc={proc.returncode} {proc.stderr[-400:]}')
    if plan is None or alone is None:
        return
    empty = plan['modules']['empty']
    check('zero mutants: the module runs whole in exactly one shard with no packages',
          ok=len(empty['shards']) == 1 and empty['packages'] == {}, detail=str(empty))
    check('zero mutants: the module costs its setup alone, so the other module shards as before',
          ok=plan['shards'] == alone['shards']
          and plan['modules']['.']['packages'] == alone['modules']['.']['packages'],
          detail=f'{plan["shards"]} vs {alone["shards"]}')
    shard = load_script('gremlins_shard', SHARD)
    check('zero mutants: the plan is valid', ok=shard.valid_plan(plan) == '',
          detail=shard.valid_plan(plan))


def test_shard_falls_back_for_a_module_it_cannot_size(tmp: Path) -> None:
    deadset = (DEADSET_GO['module'], DEADSET_GO['env'], DEADSET_GO['packages'])
    _, alone = plan_repo(tmp, 'alone', {'.': deadset})
    untimed = {'internal/a': (20, 0, None)}
    proc, _ = plan_repo(tmp, 'gap', {
        '.': deadset,
        'failed': ('github.com/cplieger/gap/failed', None, None),
        'stopped': ('github.com/cplieger/gap/stopped', {**SMALL_ENV, 'sized': None}, SMALL_PACKAGES),
        'untimed': ('github.com/cplieger/gap/untimed', SMALL_ENV, untimed),
    })
    plan = written_plan(tmp, 'gap')
    unsized = ('failed', 'stopped', 'untimed')
    check('unsized module: the planner fails and names each module it could not size',
          ok=proc.returncode != 0 and all(f'module {d} ' in proc.stderr for d in unsized)
          and '::error::' in proc.stderr, detail=f'rc={proc.returncode} {proc.stderr[-600:]}')
    check('unsized module: it still writes the plan', ok=plan is not None and alone is not None)
    if plan is None or alone is None:
        return
    shard = load_script('gremlins_shard', SHARD)
    check('unsized module: the plan is valid', ok=shard.valid_plan(plan) == '',
          detail=shard.valid_plan(plan))
    k = alone['shards']
    check('unsized module: the sized module shards exactly as it does alone',
          ok=plan['modules']['.']['packages'] == alone['modules']['.']['packages']
          and plan['modules']['.']['shards'] == list(range(1, k + 1)),
          detail=str(plan['modules']['.']['shards']))
    own = [plan['modules'][d] for d in unsized]
    check('unsized module: each runs whole and unmeasured in a shard of its own',
          ok=plan['shards'] == k + 3
          and sorted(e['shards'][0] for e in own) == [k + 1, k + 2, k + 3]
          and all(len(e['shards']) == 1 and e['packages'] == {} and e['mode'] is None
                  and e['workers'] is None for e in own),
          detail=str(own))
    check('unsized module: its shard has no projection',
          ok=plan['projected_minutes'][:k] == alone['projected_minutes']
          and plan['projected_minutes'][k:] == [None] * 3, detail=str(plan['projected_minutes']))
    path = tmp / 'gap.json'
    path.write_text(json.dumps(plan))
    proc = run_shard('modules', '--plan', str(path), '--shard', str(k + 1),
                     '--modules', '. failed stopped untimed')
    check('unsized module: its shard runs it with no plan mode or workers and skips the rest',
          ok=proc.returncode == 0 and proc.stdout.splitlines()
          == ['skip .', 'run failed', 'skip stopped', 'skip untimed'],
          detail=f'{proc.stdout!r} {proc.stderr[-300:]}')
    proc, matrix = matrix_of(tmp, plan)
    check('unsized module: the repo still shards', ok=matrix is not None and len(matrix) == k + 3,
          detail=f'{matrix} {proc.stderr[-300:]}')
    proc, _ = plan_repo(tmp, 'tiny', {'.': ('github.com/cplieger/tiny', SMALL_ENV, SMALL_PACKAGES),
                                      'failed': ('github.com/cplieger/tiny/failed', None, None)})
    plan = written_plan(tmp, 'tiny')
    check('unsized module: in a repo that fits one job it joins that job',
          ok=proc.returncode != 0 and plan is not None and plan['shards'] == 1
          and plan['modules']['failed']['shards'] == [1] and shard.valid_plan(plan) == '',
          detail=f'{plan and plan["modules"]} {proc.stderr[-300:]}')
    for label, entry in (('a mode without workers', {'mode': 'per-package', 'workers': None}),
                         ('workers without a mode', {'mode': None, 'workers': 4}),
                         ('an unknown mode', {'mode': 'sharded', 'workers': 4})):
        bad = json.loads(path.read_text())
        bad['modules']['failed'].update(entry)
        check(f'unsized module: a plan entry with {label} is refused',
              ok=shard.valid_plan(bad) != '', detail=str(entry))


def test_shard_matrix_falls_back_to_one_job(tmp: Path) -> None:
    proc, plan = plan_repo(tmp, 'deadset-go', {'.': (DEADSET_GO['module'], DEADSET_GO['env'],
                                                     DEADSET_GO['packages'])})
    if plan is None:
        check('matrix: deadset-go plan', ok=False, detail=proc.stderr[-400:])
        return
    plans = tmp / 'plans'
    gap = json.loads(json.dumps({**plan, 'repo': 'gap'}))
    gap['modules']['.']['packages']['internal/exempt']['shard'] = 99
    spaced = json.loads(json.dumps({**plan, 'repo': 'spaced'}))
    spaced['modules']['.']['packages']['a b'] = spaced['modules']['.']['packages'].pop('internal/exempt')
    for name, doc in (('shard-plan-deadset-go', plan),
                      ('shard-plan-bad', {**plan, 'repo': 'bad', 'sha': 'HEAD; rm -rf /'}),
                      ('shard-plan-gap', gap), ('shard-plan-spaced', spaced)):
        (plans / name).mkdir(parents=True)
        (plans / name / 'plan.json').write_text(json.dumps(doc))
    proc = run_shard('matrix', '--repos', json.dumps(['deadset-go', 'thing', 'bad', 'gap', 'spaced']),
                     '--plans', str(plans), '--attempts', '3',
                     '--artifacts', json.dumps(sorted(d.name for d in plans.iterdir())))
    matrix = json.loads(proc.stdout) if proc.returncode == 0 else []
    k = plan['shards']
    want = [{'repo': 'deadset-go', 'attempt': a, 'shard': s, 'shards': k}
            for a in (1, 2, 3) for s in range(1, k + 1)]
    want += [{'repo': r, 'attempt': a} for r in ('thing', 'bad', 'gap', 'spaced') for a in (1, 2, 3)]
    check('matrix: attempt x shard for a sharded repo, one job per attempt otherwise',
          ok=matrix == want, detail=f'rc={proc.returncode} {proc.stdout[:400]} {proc.stderr[-300:]}')
    check('matrix: a repo without a plan says it runs in one job',
          ok='thing' in proc.stderr and 'one job' in proc.stderr, detail=proc.stderr[-400:])
    check('matrix: a plan that fails validation is not trusted',
          ok='bad' in proc.stderr and 'sha' in proc.stderr, detail=proc.stderr[-400:])
    check('matrix: a plan that gives a package no shard its module runs is not trusted',
          ok='gap: shard plan refused' in proc.stderr and '99' in proc.stderr,
          detail=proc.stderr[-400:])
    check('matrix: a plan with a package dir the scope cannot exclude is not trusted',
          ok="spaced: shard plan refused" in proc.stderr and "'a b'" in proc.stderr,
          detail=proc.stderr[-400:])
    proc, _ = plan_repo(tmp, 'spaced-dry', {'.': ('github.com/cplieger/spaced', SMALL_ENV,
                                                  {'a b': (3, 0, 500)})})
    spaced_plan = written_plan(tmp, 'spaced-dry')
    check('matrix: the planner fails on a dry run listing a package dir it cannot scope, '
          'and runs that module whole',
          ok=proc.returncode != 0 and "'a b'" in proc.stderr and spaced_plan is not None
          and spaced_plan['modules']['.']['packages'] == {}
          and spaced_plan['modules']['.']['mode'] is None,
          detail=f'{spaced_plan} {proc.stderr[-300:]}')
    proc = run_shard('matrix', '--repos', '[]', '--plans', str(tmp / 'none'), '--attempts', '3',
                     '--artifacts', '[]')
    check('matrix: no repos is an empty matrix', ok=proc.returncode == 0 and
          json.loads(proc.stdout or 'null') == [], detail=f'{proc.stdout!r} {proc.stderr[-200:]}')


def test_shard_matrix_binds_each_plan_to_its_artifact(tmp: Path) -> None:
    proc, plan = plan_repo(tmp, 'b', {'.': (DEADSET_GO['module'], DEADSET_GO['env'],
                                            DEADSET_GO['packages'])})
    if plan is None or plan['shards'] < 2:
        check('binding: a plan of 2 or more shards', ok=False, detail=proc.stderr[-400:])
        return
    k = plan['shards']
    fewer = json.loads(json.dumps(plan))
    fewer['shards'] = 1
    for e in fewer['modules'].values():
        e['shards'] = [1]
        for p in e['packages'].values():
            p['shard'] = 1
    shard = load_script('gremlins_shard', SHARD)
    check('binding: the forged plan alone would be trusted', ok=shard.valid_plan(fewer) == '',
          detail=shard.valid_plan(fewer))

    def matrix_for(repos: list[str], files: dict[str, dict],
                   artifacts: list[str] | None = None) -> tuple[list | None, str]:
        """ARTIFACTS defaults to the top-level directories FILES names."""
        plans = tmp / f'plans-{len(list(tmp.iterdir()))}'
        for rel, doc in files.items():
            (plans / rel).parent.mkdir(parents=True, exist_ok=True)
            (plans / rel).write_text(json.dumps(doc))
        if artifacts is None:
            artifacts = sorted({rel.split('/')[0] for rel in files if '/' in rel})
        p = run_shard('matrix', '--repos', json.dumps(repos), '--plans', str(plans), '--attempts', '1',
                      '--artifacts', json.dumps(artifacts))
        return (json.loads(p.stdout) if p.returncode == 0 else None), p.stderr

    sharded_b = [{'repo': 'b', 'attempt': 1, 'shard': s, 'shards': k} for s in range(1, k + 1)]
    # 'a' and 'z' sort either side of 'b', so neither order lets a forgery win.
    for forger in ('a', 'z'):
        got, err = matrix_for([forger, 'b'], {'shard-plan-b/plan.json': plan,
                                              f'shard-plan-{forger}/plan.json': fewer})
        check(f'binding: a plan uploaded by {forger} naming b does not replace b\'s own',
              ok=got == [{'repo': forger, 'attempt': 1}, *sharded_b],
              detail=f'{got} {err[-300:]}')
        check(f'binding: the warning names {forger} and the repo its artifact claims',
              ok=f"{forger}: its shard-plan artifact names repo 'b'" in err, detail=err[-300:])
    got, err = matrix_for(['a', 'b'], {'plan.json': plan}, ['shard-plan-b'])
    check('binding: with two repos planned and one plan uploaded, the lone root plan is its uploader\'s',
          ok=got == [{'repo': 'a', 'attempt': 1}, *sharded_b], detail=f'{got} {err[-300:]}')
    for listed in ([], ['shard-plan-a', 'shard-plan-b'], ['shard-plan-c'], ['plan-b']):
        got, err = matrix_for(['a', 'b'], {'plan.json': plan}, listed)
        check(f'binding: a lone root plan with the artifacts {listed} is ignored',
              ok=got == [{'repo': 'a', 'attempt': 1}, {'repo': 'b', 'attempt': 1}]
              and 'is not one repo\'s shard-plan artifact' in err, detail=f'{got} {err[-300:]}')
    got, err = matrix_for(['a'], {'plan.json': plan}, ['shard-plan-a'])
    check('binding: a lone root plan naming another repo is ignored',
          ok=got == [{'repo': 'a', 'attempt': 1}] and "names repo 'b'" in err,
          detail=f'{got} {err[-300:]}')
    got, err = matrix_for(['b'], {'plan.json': plan}, ['shard-plan-b'])
    check('binding: a lone root plan is its one repo\'s', ok=got == sharded_b, detail=f'{got} {err[-300:]}')
    for repos in (['b'], ['a', 'b']):
        got, err = matrix_for(repos, {'plan.json': plan}, ['shard-plan-b', 'shard-plan-b'])
        check(f'binding: a lone root plan listed once per rerun attempt is still b\'s ({repos})',
              ok=got == [{'repo': r, 'attempt': 1} for r in repos if r != 'b'] + sharded_b,
              detail=f'{got} {err[-300:]}')
    got, err = matrix_for(['a', 'b'], {'shard-plan-b/nested/plan.json': plan,
                                       'shard-plan-c/plan.json': {**plan, 'repo': 'c'}})
    check('binding: a nested plan or one from a repo outside the fanout is ignored',
          ok=got == [{'repo': 'a', 'attempt': 1}, {'repo': 'b', 'attempt': 1}]
          and err.count('is not one repo\'s shard-plan artifact') == 2, detail=f'{got} {err[-300:]}')


def three_module_plan(tmp: Path) -> Path:
    """A plan whose root packages split across two shards, beside an integration module."""
    doc = {
        'repo': 'tree', 'sha': FIXTURE_SHA, 'target_minutes': 240, 'shards': 2,
        'one_job_minutes': 400.0, 'projected_minutes': [200.0, 200.0],
        'modules': {
            '.': {'mode': 'per-package', 'workers': 4, 'setup_secs': 60.0, 'shards': [1, 2],
                  'packages': {p: {'shard': s} for p, s in (('.', 1), ('a', 2), ('a/b', 1),
                                                           ('c.d', 2), ('cXd', 1))}},
            'tools': {'mode': 'integration', 'workers': 2, 'setup_secs': 60.0, 'shards': [2],
                      'packages': {}},
        },
    }
    path = tmp / 'plan.json'
    path.write_text(json.dumps(doc))
    return path


def test_shard_lists_each_shards_modules(tmp: Path) -> None:
    plan = three_module_plan(tmp)
    got = {}
    for n in (1, 2):
        proc = run_shard('modules', '--plan', str(plan), '--shard', str(n), '--modules', '. tools')
        got[n] = proc.stdout.splitlines() if proc.returncode == 0 else proc.stderr
    check('shard modules: each shard runs its modules in their planned mode and worker count, '
          'and skips the rest',
          ok=got == {1: ['run . per-package 4', 'skip tools'],
                     2: ['run . per-package 4', 'run tools integration 2']}, detail=str(got))
    for label, workers in (('zero', 0), ('missing', None), ('not a number', '4'), ('a bool', True)):
        doc = json.loads(plan.read_text())
        if workers is None:
            del doc['modules']['tools']['workers']
        else:
            doc['modules']['tools']['workers'] = workers
        bad = tmp / 'bad-workers.json'
        bad.write_text(json.dumps(doc))
        proc = run_shard('modules', '--plan', str(bad), '--shard', '1', '--modules', '. tools')
        check(f'shard modules: a plan whose worker count is {label} is refused',
              ok=proc.returncode != 0 and 'workers' in proc.stderr, detail=proc.stderr[-300:])
    proc = run_shard('modules', '--plan', str(plan), '--shard', '1', '--modules', '. tools extra')
    check('shard modules: a module set that differs from the plan fails the shard',
          ok=proc.returncode != 0 and 'extra' in proc.stderr, detail=proc.stderr[-300:])
    proc = run_shard('modules', '--plan', str(plan), '--shard', '3', '--modules', '. tools')
    check('shard modules: a shard the plan does not have fails',
          ok=proc.returncode != 0, detail=proc.stderr[-300:])


def test_shard_scope_partitions_the_module(tmp: Path) -> None:
    plan = three_module_plan(tmp)
    target = tmp / 'target'
    for name in ('root.go', 'a/a.go', 'a/b/b.go', 'a/b/b_test.go', 'c.d/c.go',
                 'cXd/look.go', 'tools/t.go'):
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        (target / name).write_text('package x\n')
    proc = run_scope(target, '.', 'tools')
    whole = gremlins_scope(target) if proc.returncode == 0 else []
    scopes = {}
    for n in (1, 2):
        proc = subprocess.run(
            [sys.executable, str(SCOPE), '--config', str(CENTRAL), '--target', str(target),
             '--plan', str(plan), '--shard', str(n), '--', '.', 'tools'],
            capture_output=True, text=True, check=False)
        scopes[n] = gremlins_scope(target) if proc.returncode == 0 else proc.stderr[-300:]
    check('shard scope: each shard mutates only its own packages, a dotted dir only itself',
          ok=scopes == {1: ['a/b/b.go', 'cXd/look.go', 'root.go'], 2: ['a/a.go', 'c.d/c.go']},
          detail=str(scopes))
    union = sorted(f for s in scopes.values() if isinstance(s, list) for f in s)
    check('shard scope: the shards together mutate exactly what one job does, once each',
          ok=union == whole, detail=f'{union} vs {whole}')
    check('shard scope: the nested module stays excluded from every shard',
          ok=all('tools/t.go' not in s for s in scopes.values()))


def test_merge_skips_a_module_the_shard_did_not_run(tmp: Path) -> None:
    root = result('github.com/cplieger/envx/v2', [('envx.go', [('KILLED', 'ARITHMETIC_BASE')])])
    proc, merged = run_merge(tmp, [('.', root)], out_name='skip.json',
                             modes={'.': 'per-package\n'}, skip=('yamlenv',))
    check('skip module: merge succeeds', ok=merged is not None, detail=proc.stderr[-300:])
    if merged is None:
        return
    check('skip module: a skipped module adds no entry and no mode',
          ok=[m['dir'] for m in merged['modules']] == ['.'] and merged['mode'] == 'per-package',
          detail=str(merged['modules']))
    nested = result('github.com/cplieger/envx/yamlenv/v2', [('y.go', [('LIVED', 'ARITHMETIC_BASE')])])
    proc, merged = run_merge(tmp, [('yamlenv', nested)], out_name='skiproot.json',
                             modes={'yamlenv': 'integration\n'}, skip=('.',))
    check('skip module: a shard that skips the root module still merges',
          ok=merged is not None and merged['mode'] == 'integration'
          and [f['file_name'] for f in merged['files']] == ['yamlenv/y.go'],
          detail=proc.stderr[-300:])
    proc, _ = run_merge(tmp, [('.', root)], out_name='skipdup.json', skip=('.',))
    check('skip module: a module both run and skipped is refused', ok=proc.returncode != 0)


def shard_docs(tmp: Path) -> tuple[dict, list[dict]]:
    """One job's merged result for a two-module repo, and the same work split over two shards."""
    files = {
        'envx.go': [('KILLED', 'ARITHMETIC_BASE'), ('LIVED', 'CONDITIONALS_BOUNDARY'),
                    ('TIMED OUT', 'INVERT_LOGICAL')],
        'parse/parse.go': [('KILLED', 'CONDITIONALS_NEGATION'), ('NOT COVERED', 'INVERT_LOOPCTRL')],
        'walk/walk.go': [('LIVED', 'ARITHMETIC_BASE'), ('NOT VIABLE', 'INVERT_BWASSIGN')],
    }
    nested = {'y.go': [('KILLED', 'ARITHMETIC_BASE'), ('KILLED', 'INVERT_LOGICAL')]}
    mod, sub = 'github.com/cplieger/envx/v2', 'github.com/cplieger/envx/yamlenv/v2'
    modes = {'.': 'per-package\n', 'yamlenv': 'integration\n'}
    _, whole = run_merge(tmp, [('.', result(mod, list(files.items()))),
                               ('yamlenv', result(sub, list(nested.items())))],
                         out_name='whole.json', modes=modes)
    _, s1 = run_merge(tmp, [('.', result(mod, [(f, files[f]) for f in ('envx.go', 'walk/walk.go')]))],
                      out_name='s1.json', modes=modes, skip=('yamlenv',))
    _, s2 = run_merge(tmp, [('.', result(mod, [('parse/parse.go', files['parse/parse.go'])])),
                            ('yamlenv', result(sub, list(nested.items())))],
                      out_name='s2.json', modes=modes)
    return whole, [s1, s2]


def test_merge_shards_reads_as_one_job(tmp: Path) -> None:
    merge_mod = load_script('gremlins_merge_shards', MERGE)
    has = hasattr(merge_mod, 'merge_shards')
    check('merge shards: gremlins-merge.py has merge_shards', ok=has)
    if not has:
        return
    whole, shards = shard_docs(tmp)
    if whole is None or None in shards:
        check('merge shards: fixture merged', ok=False)
        return
    merged = merge_mod.merge_shards(shards)
    keys = ('go_module', 'mutants_total', 'mutants_killed', 'mutants_lived', 'mutants_not_viable',
            'mutants_not_covered', 'mutator_statistics', 'mode')
    check('merge shards: counters, statistics and mode equal one job',
          ok={k: merged[k] for k in keys} == {k: whole[k] for k in keys},
          detail=f'{ {k: merged[k] for k in keys} } vs { {k: whole[k] for k in keys} }')
    check('merge shards: efficacy and coverage equal one job',
          ok=abs(merged['test_efficacy'] - whole['test_efficacy']) < 1e-9
          and abs(merged['mutations_coverage'] - whole['mutations_coverage']) < 1e-9)
    check('merge shards: every file once, as one job lists them',
          ok=sorted(merged['files'], key=lambda f: f['file_name'])
          == sorted(whole['files'], key=lambda f: f['file_name']))
    check('merge shards: per-module counters sum across shards',
          ok=sorted(merged['modules'], key=lambda m: m['dir'])
          == sorted(whole['modules'], key=lambda m: m['dir']),
          detail=f'{merged["modules"]} vs {whole["modules"]}')
    try:
        merge_mod.merge_shards([shards[0], shards[0]])
        refused = ''
    except SystemExit as exc:
        refused = str(exc)
    check('merge shards: a file measured in two shards is refused',
          ok='envx.go' in refused, detail=refused[-300:])
    bad = json.loads(json.dumps(shards[1]))
    bad['mutants_killed'] += 1
    try:
        merge_mod.merge_shards([shards[0], bad])
        refused = ''
    except SystemExit as exc:
        refused = str(exc)
    check('merge shards: a shard whose totals disagree with its files is refused',
          ok='mutants_killed' in refused, detail=refused[-300:])


def test_aggregate_merges_shards_per_attempt(tmp: Path) -> None:
    whole, shards = shard_docs(tmp)
    if whole is None or None in shards:
        check('aggregate shards: fixture merged', ok=False)
        return

    def aggregate_run(art: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(AGGREGATE), '--repo', 'envx', '--artifacts-dir', str(art),
             '--week', '2026-11-15 22:00', '--run-url', 'https://example.invalid/run/9',
             '--badge-file', str(art / 'badge.json'),
             '--attempts-marker-file', str(art / 'attempts.txt')],
            capture_output=True, text=True, check=False)

    one = tmp / 'one-job'
    for attempt in (1, 2, 3):
        (attempt_dir(one, 'envx', attempt) / 'gremlins-out.json').write_text(json.dumps(whole))
    split = tmp / 'sharded'
    for attempt in (1, 2, 3):
        for n, doc in enumerate(shards, start=1):
            d = split / f'gremlins-envx-{attempt}-shard{n}'
            d.mkdir(parents=True)
            (d / 'meta.json').write_text(json.dumps(
                {'repo': 'envx', 'attempt': str(attempt), 'shard': str(n), 'shards': '2'}))
            (d / 'gremlins-out.json').write_text(json.dumps(doc))
    want, got = aggregate_run(one), aggregate_run(split)
    check('aggregate shards: exits 0', ok=got.returncode == 0, detail=got.stderr[-400:])
    check('aggregate shards: the tracker body reads exactly as one job per attempt',
          ok=got.stdout == want.stdout and bool(want.stdout), detail=got.stdout[:600])
    check('aggregate shards: the badge reads exactly as one job per attempt',
          ok=(split / 'badge.json').read_text() == (one / 'badge.json').read_text())
    check('aggregate shards: three attempts counted, not six',
          ok=(split / 'attempts.txt').read_text() == '3')
    (split / 'gremlins-envx-3-shard2' / 'gremlins-out.json').write_text('{"gremlins_error": true}')
    got = aggregate_run(split)
    check('aggregate shards: an attempt with a failed shard is skipped, not half-counted',
          ok=got.returncode == 0 and (split / 'attempts.txt').read_text() == '2'
          and 'attempt 3' in got.stderr, detail=got.stderr[-400:])
    for p in (split / 'gremlins-envx-3-shard2').iterdir():
        p.unlink()
    (split / 'gremlins-envx-3-shard2').rmdir()
    got = aggregate_run(split)
    check('aggregate shards: an attempt missing a shard is skipped',
          ok=got.returncode == 0 and (split / 'attempts.txt').read_text() == '2'
          and 'attempt 3' in got.stderr, detail=got.stderr[-400:])


def test_runner_takes_the_mode_the_plan_set(tmp: Path) -> None:
    guards = ''.join(re.findall(r'^: "\$\{.*\n', RUNNER.read_text(), re.MULTILINE))
    base = {'PATH': '/usr/bin:/bin', **dict.fromkeys((
        'GREMLINS_VERSION', 'GREMLINS_PATCHES', 'WORKERS_MAX', 'GOMEM_MAX_MB', 'GOMEM_SOLO_MB',
        'PROBE_TIMEOUT', 'OUT'), 'x'), 'JOB_TIMEOUT_MINUTES': '350', 'GREMLINS_WORKERS': '4'}
    _, proc = call_shell('select_mode', '60', '20', '4', deps=('integration_wanted',),
                         prelude=guards, env={**base, 'GREMLINS_MODE': 'per-package'})
    check('runner forced mode: the plan overrides a fitting integration estimate',
          ok=proc.returncode == 0 and proc.stdout == 'per-package\n'
          and proc.stderr == 'mode=per-package (set by the shard plan)\n',
          detail=f'rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}')
    _, proc = call_shell('select_mode', '', '', '4', deps=('integration_wanted',),
                         prelude=guards, env={**base, 'GREMLINS_MODE': 'integration'})
    check('runner forced mode: integration without a dry-run count',
          ok=proc.returncode == 0 and proc.stdout == 'integration\n',
          detail=f'rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}')
    _, proc = call_shell('select_mode', '60', '20', '4', deps=('integration_wanted',),
                         prelude=guards, env={**base, 'GREMLINS_MODE': 'sharded'})
    check('runner forced mode: an unknown mode fails',
          ok=proc.returncode != 0 and proc.stdout == '' and 'sharded' in proc.stderr,
          detail=f'rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}')
    unpaired = {k: v for k, v in base.items() if k != 'GREMLINS_WORKERS'}
    for label, extra in (('a plan mode without the plan\'s workers', {'GREMLINS_MODE': 'per-package'}),
                         ('the plan\'s workers without its mode', {'GREMLINS_WORKERS': '4'})):
        _, proc = call_shell('select_mode', '60', '20', '4', deps=('integration_wanted',),
                             prelude=guards, env={**unpaired, **extra})
        check(f'runner forced mode: {label} is refused', ok=proc.returncode != 0 and proc.stdout == '',
              detail=f'rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}')


def stub_tools(d: Path, tools: dict[str, str]) -> str:
    """Write each `name: sh body` as an executable in d; returns a PATH with d first."""
    d.mkdir(parents=True, exist_ok=True)
    for name, body in tools.items():
        (d / name).write_text(f'#!/bin/sh\n{body}\n')
        (d / name).chmod(0o755)
    return f'{d}:/usr/bin:/bin'


def test_runner_shard_takes_the_plans_workers(tmp: Path) -> None:
    src = shell_function('choose_workers')
    check('runner workers: choose_workers extracted from gremlins-run.sh', ok=bool(src))
    if not src:
        return
    log, state = tmp / 'go.log', tmp / 'solo-passed'
    # The solo suite passes and every later run fails, as a suite that cannot
    # share /tmp with a copy of itself does.
    path = stub_tools(tmp / 'bin', {
        'go': f'echo "$*" >>"{log}"\ncase "$1" in test) [ -e "{state}" ] && exit 1; '
              f'touch "{state}" ;; esac\nexit 0',
        'gremlins': 'exit 1',
    })
    (tmp / 'mod').mkdir()
    env = {'PATH': path, 'HOME': str(tmp), 'WORKERS_MAX': '4', 'GOMEM_MAX_MB': '3000',
           'GOMEM_SOLO_MB': '12000', 'PROBE_TIMEOUT': '60'}

    def run(extra: dict[str, str]) -> subprocess.CompletedProcess:
        log.unlink(missing_ok=True)
        state.unlink(missing_ok=True)
        return subprocess.run(
            ['bash', '--norc', '--noprofile', '-c', (f'set -euo pipefail\n{src}\nchoose_workers\n'
             'printf "workers=%s GOMEMLIMIT=%s\\n" "${workers}" "${GOMEMLIMIT}"')],
            cwd=tmp / 'mod', env={**env, **extra}, capture_output=True, text=True, check=False)

    proc = run({'GREMLINS_WORKERS': '4', 'GREMLINS_MODE': 'per-package'})
    check('runner workers: a shard given the plan\'s count runs at it without probing',
          ok=proc.returncode == 0 and proc.stdout.endswith('workers=4 GOMEMLIMIT=3000MiB\n')
          and not log.exists(),
          detail=f'rc={proc.returncode} out={proc.stdout[-300:]!r} go={log.exists() and log.read_text()!r}')
    proc = run({'GREMLINS_WORKERS': '1', 'GREMLINS_MODE': 'per-package'})
    check('runner workers: a plan count of 1 takes the heap the container sized for it',
          ok=proc.returncode == 0 and proc.stdout.endswith('workers=1 GOMEMLIMIT=3000MiB\n')
          and not log.exists(), detail=f'rc={proc.returncode} out={proc.stdout[-300:]!r}')
    proc = run({})
    check('runner workers: with no plan count a suite that fails beside itself runs at 1 worker',
          ok=proc.returncode == 0 and proc.stdout.endswith('workers=1 GOMEMLIMIT=12000MiB\n')
          and log.exists() and log.read_text().count('test') == 3,
          detail=f'rc={proc.returncode} out={proc.stdout[-300:]!r} go={log.exists() and log.read_text()!r}')


def test_runner_plan_probe_ends_by_the_deadline(tmp: Path) -> None:
    srcs = [shell_function(fn) for fn in ('plan_time_left', 'choose_workers')]
    text = RUNNER.read_text()
    check('runner plan deadline: plan_time_left runs right before choose_workers',
          ok=all(srcs) and re.search(r'^plan_time_left\nchoose_workers$', text, re.MULTILINE) is not None)
    if not all(srcs):
        return
    log = tmp / 'go.log'
    path = stub_tools(tmp / 'bin', {'go': f'echo "$*" >>"{log}"', 'gremlins': 'exit 1'})
    (tmp / 'mod').mkdir()
    env = {'PATH': path, 'HOME': str(tmp), 'WORKERS_MAX': '4', 'GOMEM_MAX_MB': '3000',
           'GOMEM_SOLO_MB': '12000', 'PROBE_TIMEOUT': '600'}

    def run(extra: dict[str, str]) -> subprocess.CompletedProcess:
        log.unlink(missing_ok=True)
        return subprocess.run(
            ['bash', '--norc', '--noprofile', '-c', (f'set -euo pipefail\n{"".join(srcs)}\n'
             'plan_time_left\nprintf "probe=%s\\n" "${PROBE_TIMEOUT}"\nchoose_workers')],
            cwd=tmp / 'mod', env={**env, **extra}, capture_output=True, text=True, check=False)

    now = int(time.time())
    proc = run({'PLAN_DIR': str(tmp), 'PLAN_DEADLINE': str(now - 1)})
    check('runner plan deadline: a plan past its deadline exits before the probe',
          ok=proc.returncode == 1 and '::error::' in proc.stdout and 'probe=' not in proc.stdout
          and not log.exists(),
          detail=f'rc={proc.returncode} out={proc.stdout[-300:]!r} go={log.exists() and log.read_text()!r}')
    proc = run({'PLAN_DIR': str(tmp), 'PLAN_DEADLINE': str(now + 2)})
    check('runner plan deadline: under 3 s left is past it, so no probe phase gets timeout 0',
          ok=proc.returncode == 1 and not log.exists(), detail=f'rc={proc.returncode} out={proc.stdout[-300:]!r}')
    proc = run({'PLAN_DIR': str(tmp), 'PLAN_DEADLINE': str(now + 90)})
    probe = re.search(r'^probe=(\d+)$', proc.stdout, re.MULTILINE)
    check('runner plan deadline: each of the three probe phases gets a third of the time left',
          ok=proc.returncode == 0 and probe is not None and 28 <= int(probe.group(1)) <= 30,
          detail=f'rc={proc.returncode} out={proc.stdout[-300:]!r}')
    proc = run({'PLAN_DIR': str(tmp), 'PLAN_DEADLINE': str(now + 3600)})
    check('runner plan deadline: a PROBE_TIMEOUT under the share is kept',
          ok=proc.returncode == 0 and 'probe=600\n' in proc.stdout, detail=f'out={proc.stdout[-300:]!r}')
    proc = run({})
    check('runner plan deadline: a run with no plan keeps PROBE_TIMEOUT',
          ok=proc.returncode == 0 and 'probe=600\n' in proc.stdout, detail=f'out={proc.stdout[-300:]!r}')


def run_container(tmp: Path, extra: dict[str, str]) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run gremlins-container.sh with a stub docker; returns the process and `docker run`'s args."""
    work = tmp / 'container'
    (work / 'target').mkdir(parents=True, exist_ok=True)
    (work / 'target' / 'go.mod').write_text('module example.com/m\n\ngo 1.26.1\n')
    args = work / 'docker-run.args'
    args.unlink(missing_ok=True)
    path = stub_tools(tmp / 'container-bin', {
        'docker': f'[ "$1" = run ] || exit 0\nshift\nprintf "%s\\n" "$@" >"{args}"',
    })
    proc = subprocess.run(
        ['bash', str(CONTAINER), '.'], cwd=work, capture_output=True, text=True, check=False,
        env={'PATH': path, 'GREMLINS_VERSION': 'v0.6.0', 'PROBE_TIMEOUT': '600',
             'JOB_TIMEOUT_MINUTES': '350', 'OUT': '/work/./gremlins-out.json', **extra})
    return proc, args.read_text().splitlines() if args.exists() else []


def test_container_passes_the_plans_workers(tmp: Path) -> None:
    proc, args = run_container(tmp, {})
    sized = next((a for a in args if a.startswith('WORKERS_MAX=')), '')
    check('container workers: an unsharded run sizes its own workers and passes no plan count',
          ok=proc.returncode == 0 and bool(sized) and not any(
              a.startswith('GREMLINS_WORKERS=') for a in args),
          detail=f'rc={proc.returncode} {args} {proc.stderr[-300:]}')
    proc, args = run_container(tmp, {'GREMLINS_WORKERS': '3', 'GREMLINS_MODE': 'per-package'})
    gomem = next((a for a in args if a.startswith('GOMEM_MAX_MB=')), '=0')
    check('container workers: a shard passes the plan\'s count as its workers and sizes the heap for it',
          ok=proc.returncode == 0 and 'GREMLINS_WORKERS=3' in args and 'WORKERS_MAX=3' in args
          and int(gomem.split('=')[1]) >= 1024,
          detail=f'rc={proc.returncode} {args} {proc.stderr[-300:]}')
    for bad in ('0', '4x', '-1', '08'):
        proc, args = run_container(tmp, {'GREMLINS_WORKERS': bad, 'GREMLINS_MODE': 'per-package'})
        check(f'container workers: a plan count of {bad!r} is refused before docker runs',
              ok=proc.returncode != 0 and args == [] and 'GREMLINS_WORKERS' in proc.stderr + proc.stdout,
              detail=f'rc={proc.returncode} {args} {proc.stdout[-200:]} {proc.stderr[-200:]}')


def time_mutants(src: str, mod: Path, deadline: int, gocache: Path) -> tuple[subprocess.CompletedProcess, dict]:
    """Run the extracted time_package_mutants in mod; returns the process and its rows by path."""
    env = {**os.environ, 'GOFLAGS': '', 'GOCACHE': str(gocache), 'GOTOOLCHAIN': 'local'}
    proc = subprocess.run(['bash', '--norc', '--noprofile', '-c',
                           f'set -euo pipefail\n{src}\ntime_package_mutants {deadline}'],
                          cwd=mod, env=env, capture_output=True, text=True, check=False)
    rows = [line.split('\t') for line in proc.stdout.splitlines()]
    return proc, {r[0]: int(r[1]) if len(r) == 2 and r[1].isdigit() else r[1:] for r in rows}


def write_module(mod: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (mod / name).parent.mkdir(parents=True, exist_ok=True)
        (mod / name).write_text(text)


SLEEP_TEST = ('package {pkg}\n\nimport (\n\t"testing"\n\t"time"\n)\n\n'
              'func TestWait(t *testing.T) {{ time.Sleep({secs} * time.Second) }}\n')


def test_runner_times_each_package_mutant(tmp: Path) -> None:
    src = shell_function('time_package_mutants')
    check('runner mutant times: time_package_mutants extracted from gremlins-run.sh', ok=bool(src))
    if not src:
        return
    mod = tmp / 'mod'
    write_module(mod, {
        'go.mod': 'module example.com/m\n\ngo 1.21\n',
        'm.go': 'package m\n\nfunc M() int { return 1 }\n',
        'sub/s.go': 'package sub\n\nfunc S() int { return 2 }\n',
        'sub/s_test.go': 'package sub\n\nimport "testing"\n\nfunc TestS(t *testing.T) {}\n',
        'broken/b.go': 'package broken\n\nfunc B() int { return 3 }\n',
        'broken/b_test.go': 'package broken\n\nimport "testing"\n\n'
                            'func TestB(t *testing.T) { t.Fatal("red") }\n',
        'onlytests/x_test.go': 'package onlytests\n',
        'wait/w.go': 'package wait\n\nfunc W() int { return 4 }\n',
        'wait/w_test.go': SLEEP_TEST.format(pkg='wait', secs=2),
    })
    before = {p: p.read_text() for p in mod.rglob('*') if p.is_file()}
    proc, rows = time_mutants(src, mod, int(time.time()) + 600, tmp / 'gocache')
    check('runner mutant times: one millisecond row per package whose tests pass',
          ok=proc.returncode == 0
          and list(rows) == ['example.com/m', 'example.com/m/sub', 'example.com/m/wait']
          and all(isinstance(ms, int) and ms > 0 for ms in rows.values()),
          detail=f'rc={proc.returncode} {rows} {proc.stderr[-300:]}')
    check('runner mutant times: a test that waits is charged at least its wall time',
          ok=isinstance(rows.get('example.com/m/wait'), int) and rows['example.com/m/wait'] >= 2000,
          detail=str(rows.get('example.com/m/wait')))
    check('runner mutant times: every file is left as it was',
          ok={p: p.read_text() for p in mod.rglob('*') if p.is_file()} == before)
    proc, rows = time_mutants(src, mod, int(time.time()) - 1, tmp / 'gocache')
    check('runner mutant times: past the plan deadline no package is timed, and it says so',
          ok=proc.returncode == 0 and rows == {} and 'deadline' in proc.stderr
          and {p: p.read_text() for p in mod.rglob('*') if p.is_file()} == before,
          detail=f'rc={proc.returncode} {rows} {proc.stderr[-300:]}')


def test_runner_marks_a_finished_plan(tmp: Path) -> None:
    text = RUNNER.read_text()
    block = re.search(r'^if \[ -n "\$\{PLAN_DIR:-\}" \]; then\n(.*?)^fi$', text, re.MULTILINE | re.DOTALL)
    tail = [line.strip() for line in block.group(1).splitlines()][-2:] if block else []
    check('runner plan: the plan says it finished only as its last write before exiting',
          ok=tail == ['printf \'sized=1\\n\' >>"${PLAN_DIR}/plan.env"', 'exit 0']
          and text.count('sized=') == 1, detail=str(tail))


def workflow_jobs() -> dict:
    import yaml
    return yaml.safe_load(WORKFLOW.read_text())['jobs']


def test_workflow_plans_then_shards_the_run(tmp: Path) -> None:
    import workflow_replay as wr
    jobs = workflow_jobs()
    check('workflow shards: fanout, plan and shard jobs feed the run',
          ok={'fanout', 'plan', 'shard', 'run'} <= set(jobs)
          and jobs.get('run', {}).get('needs') == ['fanout', 'shard'], detail=str(list(jobs)))
    if not {'plan', 'shard', 'run'} <= set(jobs):
        return
    check('workflow shards: notify needs every other job',
          ok=sorted(jobs['notify']['needs']) == sorted(set(jobs) - {'notify'}),
          detail=str(jobs['notify']['needs']))

    def steps(job: str) -> dict[str, str]:
        return {s['name']: s.get('run', '') for s in jobs[job]['steps'] if 'name' in s}

    plan_steps, run_steps = steps('plan'), steps('run')
    prep = 'Clone the target repo and scope its modules'
    gone = ('Clone target repo', 'Discover Go modules to measure',
            'Sync central .gremlins.yaml (authoritative for the weekly run)')
    check('workflow shards: the plan and run jobs prepare the target through one script',
          ok=all('scripts/gremlins-prepare.sh' in s.get(prep, '') for s in (plan_steps, run_steps))
          and not any(n in s for s in (plan_steps, run_steps) for n in gone),
          detail=f'{plan_steps.get(prep)!r} {run_steps.get(prep)!r}')
    (tmp / 'wire' / 'scripts').mkdir(parents=True)
    stub = tmp / 'wire' / 'scripts' / 'gremlins-prepare.sh'
    stub.write_text('printf \'%s\\n\' "$@" >args\n')
    for label, body, env, want in (
        ('the plan job prepares the head', plan_steps.get(prep, ''), {}, ['x']),
        ('an unsharded run prepares the head', run_steps.get(prep, ''),
         {'SHARDS': '', 'SHARD': ''}, ['x']),
        ('a shard prepares from its plan', run_steps.get(prep, ''),
         {'SHARDS': '3', 'SHARD': '2'}, ['x', 'plan/plan.json', '2']),
    ):
        (tmp / 'wire' / 'args').unlink(missing_ok=True)
        proc = subprocess.run(['bash', '-e', '-c', body], cwd=tmp / 'wire',
                              env={**os.environ, 'REPO': 'x', **env}, capture_output=True,
                              text=True, check=False)
        out = tmp / 'wire' / 'args'
        args = out.read_text().splitlines() if proc.returncode == 0 and out.exists() else None
        check(f'workflow shards: {label}', ok=args == want, detail=f'{args} {proc.stderr[-200:]}')
    size = next((s for s in jobs['plan']['steps'] if s.get('name') == "Size each module's mutation work"), {})
    cap = '${{ needs.fanout.outputs.plan_timeout_minutes }}'
    check('workflow shards: the plan deadline derives from the plan job\'s own cap',
          ok=str(jobs['plan'].get('timeout-minutes')) == '${{ fromJSON(needs.fanout.outputs.plan_timeout_minutes) }}'
          and size.get('env', {}).get('PLAN_TIMEOUT_MINUTES') == cap,
          detail=f'{jobs["plan"].get("timeout-minutes")} {size.get("env")}')
    sized = tmp / 'size'
    stub_tools(sized / 'scripts', {})
    (sized / 'scripts' / 'gremlins-container.sh').write_text(
        'printf \'%s|%s|%s\\n\' "$1" "${PLAN_DEADLINE:-}" "${PLAN_DIR#"$PWD"/}" >>container.log\n')
    (sized / 'scripts' / 'gremlins-shard.py').write_text(
        'import sys\nopen("shard.args", "w").write(" ".join(sys.argv[1:]))\n')
    start = int(time.time())
    proc = subprocess.run(
        ['bash', '-c', size.get('run', 'false')], cwd=sized, capture_output=True, text=True,
        check=False, env={**os.environ, 'REPO': 'x', 'SHA': FIXTURE_SHA, 'MODULES': '. tools',
                          'JOB_TIMEOUT_MINUTES': '350', 'TARGET_MINUTES': '240',
                          'PLAN_TIMEOUT_MINUTES': '90'})
    calls = [line.split('|') for line in (sized / 'container.log').read_text().splitlines()] if (
        sized / 'container.log').exists() else []
    deadlines = {int(c[1]) for c in calls if len(c) == 3 and c[1].isdigit()}
    check('workflow shards: every module is sized against one deadline inside the plan job\'s cap',
          ok=proc.returncode == 0 and [(c[0], c[-1]) for c in calls]
          == [('.', 'plan/modules/.'), ('tools', 'plan/modules/tools')] and len(deadlines) == 1
          and start + 50 * 60 <= min(deadlines) <= int(time.time()) + 80 * 60,
          detail=f'rc={proc.returncode} {calls} start={start} {proc.stderr[-300:]}')
    shard_args = (sized / 'shard.args').read_text() if (sized / 'shard.args').exists() else ''
    check('workflow shards: the plan step hands every module to the planner',
          ok='--module .=plan/modules/. --module tools=plan/modules/tools' in shard_args
          and '--target-minutes 240' in shard_args, detail=shard_args)
    (sized / 'scripts' / 'gremlins-container.sh').write_text(
        'echo "$1" >>container.log\n[ "$1" != "${FAIL_MODULE:-}" ]\n')
    for label, extra, want_calls, want_errors in (
        ('a module whose sizing fails leaves the rest sized and every module planned',
         {'FAIL_MODULE': '.', 'PLAN_TIMEOUT_MINUTES': '90'}, ['.', 'tools'], ['.']),
        ('no module starts after the plan deadline',
         {'PLAN_TIMEOUT_MINUTES': '20'}, [], ['.', 'tools']),
    ):
        for name in ('container.log', 'shard.args'):
            (sized / name).unlink(missing_ok=True)
        proc = subprocess.run(
            ['bash', '-c', size.get('run', 'false')], cwd=sized, capture_output=True, text=True,
            check=False, env={**os.environ, 'REPO': 'x', 'SHA': FIXTURE_SHA, 'MODULES': '. tools',
                              'JOB_TIMEOUT_MINUTES': '350', 'TARGET_MINUTES': '240', **extra})
        calls = (sized / 'container.log').read_text().splitlines() if (
            sized / 'container.log').exists() else []
        errors = re.findall(r'^::error::.*module (\S+)', proc.stdout, re.MULTILINE)
        shard_args = (sized / 'shard.args').read_text() if (sized / 'shard.args').exists() else ''
        check(f'workflow shards: {label}',
              ok=proc.returncode == 0 and calls == want_calls and errors == want_errors
              and '--module .=plan/modules/. --module tools=plan/modules/tools' in shard_args,
              detail=f'rc={proc.returncode} {calls} {errors} {shard_args!r} {proc.stdout[-300:]}')

    def render(template: str, matrix: dict) -> str:
        return wr.Scope({'matrix': matrix}, {}, []).render(template)

    gremlins_step = next((s.get('run', '') for s in jobs['run']['steps']
                          if s.get('name', '').startswith('Run gremlins')), '')
    ran = tmp / 'run'
    stub_tools(ran / 'scripts', {})
    (ran / 'scripts' / 'gremlins-shard.py').write_text(
        'import os, sys\nopen("shard.args", "w").write(" ".join(sys.argv[1:]))\n'
        'print(os.environ.get("PLAN_LINES", "run . per-package 4\\nskip tools"))\n')
    (ran / 'scripts' / 'gremlins-container.sh').write_text(
        'printf \'%s|%s|%s|%s\\n\' "$1" "${GREMLINS_MODE-unset}" "${GREMLINS_WORKERS-unset}" '
        '"${OUT}" >>container.log\n')
    (ran / 'scripts' / 'gremlins-merge.py').write_text(
        'import sys\nopen("merge.args", "w").write(" ".join(sys.argv[1:]))\n')
    for label, matrix, env, want_container, want_merge in (
        ('a shard runs its planned modules at the plan\'s mode and workers and skips the rest',
         {'repo': 'x', 'attempt': 1, 'shard': 2, 'shards': 3}, {'SHARDS': '3', 'SHARD': '2'},
         ['.|per-package|4|/work/./gremlins-out.json'],
         '--skip-module tools --module .=target/./gremlins-out.json'),
        ('a shard runs a module the plan could not size with no plan mode or workers',
         {'repo': 'x', 'attempt': 1, 'shard': 2, 'shards': 3},
         {'SHARDS': '3', 'SHARD': '2', 'PLAN_LINES': 'skip .\nrun tools'},
         ['tools|||/work/tools/gremlins-out.json'],
         '--skip-module . --module tools=target/tools/gremlins-out.json'),
        ('an unsharded attempt runs every module with no plan mode or workers',
         {'repo': 'x', 'attempt': 1}, {'SHARDS': '', 'SHARD': ''},
         ['.|||/work/./gremlins-out.json', 'tools|||/work/tools/gremlins-out.json'],
         '--module .=target/./gremlins-out.json --module tools=target/tools/gremlins-out.json'),
    ):
        for name in ('container.log', 'merge.args', 'shard.args'):
            (ran / name).unlink(missing_ok=True)
        body = render(gremlins_step, matrix).replace('/tmp/gremlins-out.json', str(ran / 'out.json'))
        proc = subprocess.run(['bash', '-c', body], cwd=ran, capture_output=True, text=True,
                              check=False, env={**os.environ, 'MODULES': '. tools',
                                                'JOB_TIMEOUT_MINUTES': '350', **env})
        got = (ran / 'container.log').read_text().splitlines() if (ran / 'container.log').exists() else []
        merge = (ran / 'merge.args').read_text() if (ran / 'merge.args').exists() else ''
        check(f'workflow shards: {label}',
              ok=proc.returncode == 0 and got == want_container
              and merge == f'--out {ran / "out.json"} {want_merge}',
              detail=f'rc={proc.returncode} {got} {merge!r} {proc.stdout[-200:]} {proc.stderr[-200:]}')
        if env['SHARDS']:
            asked = (ran / 'shard.args').read_text() if (ran / 'shard.args').exists() else ''
            check('workflow shards: a shard asks the plan for its own shard\'s modules',
                  ok=asked == 'modules --plan plan/plan.json --shard 2 --modules . tools',
                  detail=asked)

    upload = next(s for s in jobs['run']['steps'] if s.get('uses', '').startswith(
        'actions/upload-artifact'))
    unsharded, sharded = {'repo': 'x', 'attempt': 1}, {'repo': 'x', 'attempt': 1, 'shard': 2,
                                                         'shards': 3}
    check('workflow shards: an unsharded job keeps its name and artifact',
          ok=render(jobs['run']['name'], unsharded) == 'run (x, 1)'
          and render(upload['with']['name'], unsharded) == 'gremlins-x-1',
          detail=f'{render(jobs["run"]["name"], unsharded)} {render(upload["with"]["name"], unsharded)}')
    check('workflow shards: a shard job names its shard',
          ok=render(jobs['run']['name'], sharded) == 'run (x, 1, shard 2 of 3)'
          and render(upload['with']['name'], sharded) == 'gremlins-x-1-shard2',
          detail=f'{render(jobs["run"]["name"], sharded)} {render(upload["with"]["name"], sharded)}')
    meta = steps('run')['Record which repo and attempt this artifact is']
    for label, env, want in (
        ('an unsharded attempt', {'REPO': 'x', 'ATTEMPT': '1', 'SHARD': '', 'SHARDS': ''},
         {'repo': 'x', 'attempt': '1'}),
        ('a shard', {'REPO': 'x', 'ATTEMPT': '1', 'SHARD': '2', 'SHARDS': '3'},
         {'repo': 'x', 'attempt': '1', 'shard': '2', 'shards': '3'}),
    ):
        out = tmp / 'meta'
        out.mkdir(exist_ok=True)
        body = meta.replace('/tmp/meta.json', str(out / 'meta.json'))
        proc = subprocess.run(['bash', '-c', body], env={**os.environ, **env},
                              capture_output=True, text=True, check=False)
        got = json.loads((out / 'meta.json').read_text()) if proc.returncode == 0 else None
        check(f'workflow shards: meta.json for {label}', ok=got == want,
              detail=f'{got} {proc.stderr[-200:]}')


def main() -> int:
    if not all(p.exists() for p in (MERGE, AGGREGATE, RUNNER, SCOPE)):
        print(f'missing script: {MERGE} / {AGGREGATE} / {RUNNER} / {SCOPE}')
        return 1
    tests = [
        test_single_module_is_identity,
        test_upstream_fixture_roundtrips,
        test_nested_module_is_measured_not_counted_uncovered,
        test_nested_verdict_in_root_run_is_fatal,
        test_inconsistent_input_is_fatal,
        test_missing_module_output_is_zero_not_error,
        test_module_nested_in_nested,
        test_root_module_required,
        test_prepare_scopes_root_run_off_nested_module,
        test_scope_excludes_every_deeper_module,
        test_scope_excludes_an_unmeasured_module,
        test_scope_takes_a_dash_dir_as_a_module,
        test_prepare_pins_a_shard_to_its_plan,
        test_prepare_clones_anonymously,
        test_scope_refuses_what_it_cannot_scope,
        test_central_config_keys_are_read,
        test_aggregate_consumes_merged_output,
        test_aggregate_flags_disagreeing_verdicts,
        test_aggregate_flags_sudden_perfect_week,
        test_aggregate_never_names_an_older_week_as_last_week,
        test_aggregate_reads_last_week_from_the_newest_row,
        test_aggregate_quiet_on_a_stable_week,
        test_aggregate_rerun_replaces_its_own_row,
        test_aggregate_reads_a_flat_single_artifact,
        test_aggregate_skips_an_attempt_it_cannot_attribute,
        test_aggregate_ignores_another_repos_attempt,
        test_timeout_coefficient_scales_with_workers,
        test_integration_decision,
        test_runner_mode_selection,
        test_workflow_applies_every_patch,
        test_merge_records_the_mode,
        test_aggregate_marks_a_mode_switch,
        test_aggregate_starts_a_baseline_when_the_mutators_change,
        test_enabled_mutators_follows_gremlins,
        test_shard_projects_deadset_go_past_one_job,
        test_shard_bin_pack_longest_first,
        test_shard_small_module_stays_one_unchanged_job,
        test_shard_never_splits_an_integration_module,
        test_shard_sizes_a_zero_mutant_module_as_zero,
        test_shard_falls_back_for_a_module_it_cannot_size,
        test_shard_matrix_falls_back_to_one_job,
        test_shard_matrix_binds_each_plan_to_its_artifact,
        test_shard_lists_each_shards_modules,
        test_shard_scope_partitions_the_module,
        test_merge_skips_a_module_the_shard_did_not_run,
        test_merge_shards_reads_as_one_job,
        test_aggregate_merges_shards_per_attempt,
        test_runner_takes_the_mode_the_plan_set,
        test_runner_shard_takes_the_plans_workers,
        test_runner_plan_probe_ends_by_the_deadline,
        test_container_passes_the_plans_workers,
        test_runner_times_each_package_mutant,
        test_runner_marks_a_finished_plan,
        test_workflow_plans_then_shards_the_run,
    ]
    for t in tests:
        print(f'{t.__name__}:')
        with tempfile.TemporaryDirectory() as td:
            t(Path(td))
    print()
    if FAILURES:
        print(f'FAILED ({len(FAILURES)}): ' + ', '.join(FAILURES))
        return 1
    print('all gremlins script checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
