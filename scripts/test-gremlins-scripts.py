#!/usr/bin/env python3
"""Pin the contract of the gremlins measurement scripts; exit 0 = pass.

gremlins-scope.py writes each module's config with its deeper modules excluded,
gremlins-merge.py folds one result per Go module into one per-attempt result,
gremlins-aggregate.py turns the attempts into the tracker body, history and
badge, and gremlins-run.sh's coefficient and mode decision are extracted and
run. Nothing else executes them before the weekly run publishes their number.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MERGE = HERE / 'gremlins-merge.py'
AGGREGATE = HERE / 'gremlins-aggregate.py'
RUNNER = HERE / 'gremlins-run.sh'
SCOPE = HERE / 'gremlins-scope.py'
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
        'INVERT_BWASSIGN': 'invert_bitwise_assignments',
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
              modes: dict[str, str] | None = None):
    """Write each module's JSON (None = no file at all) and merge them.

    modes: {dir: text} written as the `<result>.mode` file gremlins-run.sh leaves.
    """
    args = []
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
# gremlins-scope.py and the workflow steps that call it
# ---------------------------------------------------------------------------
def workflow_step(name: str) -> str:
    """The `run: |` block of weekly-gremlins.yaml's step `name`, dedented; '' when absent."""
    lines = WORKFLOW.read_text().splitlines()
    try:
        start = lines.index(f'      - name: {name}')
    except ValueError:
        return ''
    for i in range(start + 1, len(lines)):
        if lines[i].startswith('      - '):
            return ''
        if lines[i] == '        run: |':
            body = []
            for line in lines[i + 1:]:
                if line and not line.startswith(' ' * 10):
                    break
                body.append(line[10:])
            return '\n'.join(body) + '\n'
    return ''


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
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t']
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run([*git, 'add', '--', *files], check=True)
    subprocess.run([*git, 'commit', '-qm', 'fixture'], check=True)


def run_scope(target: Path, *modules: str, config: Path = CENTRAL,
              excludes: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCOPE), '--config', str(config), '--target', str(target),
         *(f'--exclude-dir={d}' for d in excludes), '--', *modules],
        capture_output=True, text=True, check=False,
    )


def test_workflow_scopes_root_run_off_nested_module(tmp: Path) -> None:
    """The animap failure, through the workflow's own Discover and Sync steps.

    The root tests compile mapread/*.go through the replace, so a root run that
    mutates them records real verdicts the merge must refuse. The root's
    gremlins scope has to stop at the nested module's directory, and at the
    static/ui module Discover prunes and so never measures.
    """
    discover = workflow_step('Discover Go modules to measure')
    sync = workflow_step('Sync central .gremlins.yaml (authoritative for the weekly run)')
    check('workflow scope: Discover and Sync steps extracted', ok=bool(discover and sync))
    if not (discover and sync):
        return
    (tmp / 'target').mkdir()
    animap_fixture(tmp / 'target')
    (tmp / 'scripts').symlink_to(HERE)
    (tmp / 'configs').symlink_to(CENTRAL.parent)
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GITHUB_OUTPUT': str(tmp / 'out')}
    bash = ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c']
    proc = subprocess.run([*bash, discover], cwd=tmp, env=env, capture_output=True, text=True,
                          check=False)
    outputs = dict(line.split('=', 1) for line in
                   ((tmp / 'out').read_text().splitlines() if (tmp / 'out').exists() else []))
    check('workflow scope: Discover measures the root and mapread, not the pruned module',
          ok=proc.returncode == 0 and outputs == {'modules': '. mapread', 'unmeasured': 'static/ui'},
          detail=f'rc={proc.returncode} {outputs!r} {proc.stderr[-300:]}')
    proc = subprocess.run([*bash, sync], cwd=tmp,
                          env={**env, 'MODULES': outputs.get('modules', ''),
                               'UNMEASURED': outputs.get('unmeasured', '')},
                          capture_output=True, text=True, check=False)
    check('workflow scope: Sync succeeds', ok=proc.returncode == 0, detail=proc.stderr[-300:])
    if proc.returncode != 0:
        return
    root_scope = gremlins_scope(tmp / 'target')
    check('workflow scope: the root run mutates no file of the nested module',
          ok=root_scope == ['animap.go', 'cmd/animap/verify.go', 'mapreader/reader.go'],
          detail=str(root_scope))
    check('workflow scope: the pruned module gets no config of its own',
          ok=not (tmp / 'target' / 'static' / 'ui' / '.gremlins.yaml').exists())
    nested_scope = gremlins_scope(tmp / 'target' / 'mapread')
    check('workflow scope: the nested run still mutates its own files',
          ok=nested_scope == ['decode.go', 'encode.go'], detail=str(nested_scope))
    check('workflow scope: a leaf module gets the central config unchanged',
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
    sync = workflow_step('Sync central .gremlins.yaml (authoritative for the weekly run)')
    (tmp / 'target' / '-h').mkdir(parents=True)
    (tmp / 'scripts').symlink_to(HERE)
    (tmp / 'configs').symlink_to(CENTRAL.parent)
    proc = subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', sync],
                          cwd=tmp, env={**os.environ, 'MODULES': ' . -h', 'UNMEASURED': ''},
                          capture_output=True, text=True, check=False)
    check('scope dash dir: the workflow Sync step passes it as a dir',
          ok=proc.returncode == 0 and (tmp / 'target' / '-h' / '.gremlins.yaml').is_file(),
          detail=f'rc={proc.returncode} {proc.stderr[-300:]}')


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
         '--existing-body-file', str(existing)],
        capture_output=True, text=True, check=False,
    )
    check('sudden perfect week: exits 0', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    check('sudden perfect week: body carries the caution',
          ok='100%' in proc.stdout and 'measurement' in proc.stdout.lower(),
          detail=proc.stdout[:600])
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
         '--existing-body-file', str(existing)],
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
         '--existing-body-file', str(existing)],
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


def call_shell(name: str, *args: str, deps: tuple[str, ...] = (), prelude: str = '',
               env: dict[str, str] | None = None) -> tuple[str, subprocess.CompletedProcess]:
    """Run gremlins-run.sh's own `name` (after `prelude` and `deps`) under its shell options.

    Returns the extracted `name` source, empty when it or a dep was not found.
    """
    text = RUNNER.read_text()
    srcs = []
    for fn in (*deps, name):
        m = re.search(rf'^{fn}\(\) {{\n.*?^}}\n', text, re.MULTILINE | re.DOTALL)
        srcs.append(m.group(0) if m else '')
    body = prelude + '\n'.join(srcs)
    proc = subprocess.run(
        ['bash', '--norc', '--noprofile', '-c', f'set -euo pipefail\n{body}\n{name} "$@"',
         name, *args],
        capture_output=True, text=True, check=False, env=env,
    )
    return srcs[-1] if all(srcs) else '', proc


def test_timeout_coefficient_scales_with_workers(_tmp: Path) -> None:
    src, _ = call_shell('timeout_coefficient', '1')
    check('coefficient: function extracted from gremlins-run.sh', ok=bool(src))
    if not src:
        return
    for workers, want in (('4', '12'), ('2', '6'), ('1', '3')):
        _, proc = call_shell('timeout_coefficient', workers)
        check(f'coefficient: {workers} worker(s) -> {want}',
              ok=proc.returncode == 0 and proc.stdout.strip() == want,
              detail=f'rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}')


def test_integration_decision(_tmp: Path) -> None:
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


def test_runner_mode_selection(_tmp: Path) -> None:
    guards = ''.join(re.findall(r'^: "\$\{.*\n', RUNNER.read_text(), re.MULTILINE))
    check('runner mode: env guards extracted from gremlins-run.sh', ok='OUT:?' in guards,
          detail=repr(guards))
    base = {'PATH': '/usr/bin:/bin', **dict.fromkeys((
        'GREMLINS_VERSION', 'GREMLINS_PATCH', 'WORKERS_MAX', 'GOMEM_MAX_MB', 'GOMEM_SOLO_MB',
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
    '| Run (UTC) | Mean efficacy | Stddev | Mutant coverage | Live mutants | Δ efficacy |\n'
    '|---|---|---|---|---|---|\n'
    '| 2026-09-27 22:00 | 90.0% | ±0.0% | 60.0% | 2 | +0.0% | <!-- run:3 -->\n'
    '| 2026-09-20 22:00 | 90.0% | ±0.0% | 60.0% | 2 | — | <!-- run:2 -->\n'
    '<!-- /gremlins-data -->\n'
)


def aggregate_week(tmp: Path, name: str, mode: str, files: list, existing: str,
                   run_id: str) -> tuple[subprocess.CompletedProcess, str]:
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
         '--existing-body-file', str(body), '--regression-marker-file', str(marker)],
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
          ok='| per-package | <!-- run:4 -->' in proc.stdout, detail=proc.stdout[:900])

    proc, flag = aggregate_week(tmp, 'switch', 'integration', half, MODE_HISTORY, '4')
    check('mode switch: switch week exits 0', ok=proc.returncode == 0, detail=proc.stderr[-400:])
    check('mode switch: the row marks the switch and the expected jump',
          ok='| integration (switched from per-package; score jump expected) | <!-- run:4 -->'
          in proc.stdout, detail=proc.stdout[:900])
    check('mode switch: the switch week does not raise the label', ok=flag == 'false', detail=flag)
    check('mode switch: the header says why', ok='Mode switched from per-package to integration'
          in proc.stdout, detail=proc.stdout[:900])

    # Re-aggregating the same run must still see the switch, not its own row.
    rerun, flag = aggregate_week(tmp, 'rerun', 'integration', half, proc.stdout, '4')
    check('mode switch: a re-aggregate of the switch run keeps the mark',
          ok='(switched from per-package; score jump expected) | <!-- run:4 -->' in rerun.stdout
          and rerun.stdout.count('<!-- run:4 -->') == 1 and flag == 'false',
          detail=rerun.stdout[:900])

    after, flag = aggregate_week(tmp, 'after', 'integration', half, proc.stdout, '5')
    check('mode switch: the next week in the new mode is unmarked',
          ok='| integration | <!-- run:5 -->' in after.stdout, detail=after.stdout[:900])
    check('mode switch: the next week compares only integration rows', ok=flag == 'false',
          detail=flag)

    perfect = [('a.go', [('KILLED', 'ARITHMETIC_BASE'), ('KILLED', 'CONDITIONALS_BOUNDARY')])]
    proc, _ = aggregate_week(tmp, 'perfect', 'integration', perfect, MODE_HISTORY, '4')
    check('mode switch: a switch to 100% is not called a measurement failure',
          ok='jump to 100%' not in proc.stdout.lower() and 'Mode switched' in proc.stdout,
          detail=proc.stdout[:900])


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
        test_workflow_scopes_root_run_off_nested_module,
        test_scope_excludes_every_deeper_module,
        test_scope_excludes_an_unmeasured_module,
        test_scope_takes_a_dash_dir_as_a_module,
        test_scope_refuses_what_it_cannot_scope,
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
        test_merge_records_the_mode,
        test_aggregate_marks_a_mode_switch,
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
