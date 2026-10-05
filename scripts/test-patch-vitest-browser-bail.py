#!/usr/bin/env python3
"""Pin the contract of patch-vitest-browser-bail.py; exit 0 means every check passed."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATCHER = HERE / 'patch-vitest-browser-bail.py'

ENSURE = '\t\t\tconst pool = ensurePool(project);\n'
CLEAR = '\t\t\tvitest.state.clearFiles(project, files.map((f) => f.filepath));'
# The exact line the stopgap must insert, byte for byte.
INSERTED = '\t\t\tvitest.onCancel(() => pool.cancel());\n'
STOCK = ENSURE + CLEAR
PATCHED = ENSURE + INSERTED + CLEAR
STAMP = '.patch-vitest-browser-bail'

# The per-run shape a fix would take: the listener moves into runWorkspaceTests.
UPSTREAM_FIXED = (
    '\t\tvitest.onCancel(() => {\n'
    '\t\t\tisCancelled = true;\n'
    '\t\t});\n'
    '\t\tvitest.onCancel(() => pool.cancel());\n'
    '\t\tconst pool = ensurePool(project);\n'
    '\t\tvitest.state.clearFiles(project, files.map((f) => f.filepath));'
)

FAILURES: list[str] = []


def check(name: str, *, ok: bool, detail: str = '') -> None:
    if ok:
        print(f'  PASS  {name}')
    else:
        print(f'  FAIL  {name}{": " + detail if detail else ""}')
        FAILURES.append(name)


def browser_pool(body: str) -> str:
    """A stand-in for vitest's createBrowserPool, shaped like the real dist."""
    return f"""function createBrowserPool(vitest) {{
\tconst projectPools = /* @__PURE__ */ new WeakMap();
\tconst ensurePool = (project) => {{
\t\tif (projectPools.has(project)) return projectPools.get(project);
\t\tconst pool = new BrowserPool(project, {{ maxWorkers: 1 }});
\t\tprojectPools.set(project, pool);
\t\tvitest.onCancel(() => {{
\t\t\tpool.cancel();
\t\t}});
\t\treturn pool;
\t}};
\tconst runWorkspaceTests = async (method, specs) => {{
\t\tconst initialisedPools = await Promise.all(Array.from(groupedFiles.entries(), async ([project, files]) => {{
\t\t\tif (isCancelled) return;
{body}
\t\t\tproviders.add(project.browser.provider);
\t\t}}));
\t}};
}}
"""


def make_tree(
    root: Path,
    *,
    vitest_version: str | None = '5.0.3',
    chunks: dict[str, str] | None = None,
) -> Path:
    vitest = root / 'node_modules' / 'vitest'
    chunk_dir = vitest / 'dist' / 'chunks'
    if vitest_version is None:
        return chunk_dir
    chunk_dir.mkdir(parents=True)
    (vitest / 'package.json').write_text(
        f'{{"name": "vitest", "version": "{vitest_version}"}}', encoding='utf-8'
    )
    if chunks is None:
        chunks = {'index.AbC123.js': 'export {};\n' + browser_pool(STOCK)}
    for name, text in chunks.items():
        (chunk_dir / name).write_bytes(text.encode('utf-8'))
    return chunk_dir


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PATCHER), '--package-dir', str(root), *args],
        capture_output=True,
        text=True,
    )


def snapshot(chunk_dir: Path) -> dict[str, bytes]:
    """Every file under node_modules/vitest, the stamp included."""
    vitest = chunk_dir.parents[1]
    return {
        str(p.relative_to(vitest)): p.read_bytes() for p in sorted(vitest.rglob('*')) if p.is_file()
    }


def case_patches_stock_chunk() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(root)
        chunk = chunk_dir / 'index.AbC123.js'
        first = run(root)
        text = chunk.read_text(encoding='utf-8')
        check('stock chunk exits 0', ok=first.returncode == 0, detail=first.stdout)
        check('reports the vitest version', ok='vitest 5.0.3' in first.stdout)
        check(
            'chunk is exactly the stock fixture with the listener inserted',
            ok=text == 'export {};\n' + browser_pool(PATCHED),
        )
        check(
            'listener sits directly after ensurePool',
            ok=text.count(ENSURE + INSERTED) == 1 and STOCK not in text,
        )
        stamp = chunk_dir.parents[1] / STAMP
        check('patch writes the provenance stamp', ok=stamp.is_file())
        # Idempotent: a workflow rerun or a second local invocation is a no-op.
        second = run(root)
        check('rerun exits 0', ok=second.returncode == 0, detail=second.stdout)
        check('rerun reports already patched', ok='already patched' in second.stdout)
        check('rerun changed nothing', ok=chunk.read_text(encoding='utf-8') == text)


def case_stale_stamp_is_not_ours() -> None:
    """A stamp for other bytes must not turn upstream's identical fix into "already patched"."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(root, chunks={'index.Stale1.js': browser_pool(PATCHED)})
        (chunk_dir.parents[1] / STAMP).write_text(
            '0' * 64 + '  dist/chunks/index.Stale1.js\n', encoding='utf-8'
        )
        before = snapshot(chunk_dir)
        proc = run(root)
        check('stale stamp exits 3', ok=proc.returncode == 3, detail=f'rc={proc.returncode}')
        check('stale stamp is not already patched', ok='already patched' not in proc.stdout)
        check('stale stamp writes nothing', ok=snapshot(chunk_dir) == before)


def case_unreadable_stamp_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(root)
        (chunk_dir.parents[1] / STAMP).mkdir()
        before = snapshot(chunk_dir)
        proc = run(root)
        check('unreadable stamp exits 1', ok=proc.returncode == 1, detail=f'rc={proc.returncode}')
        check('unreadable stamp writes nothing', ok=snapshot(chunk_dir) == before)


def case_check_mode_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(root)
        before = snapshot(chunk_dir)
        proc = run(root, '--check')
        check('--check exits 0', ok=proc.returncode == 0, detail=proc.stdout)
        check('--check leaves the chunk untouched', ok=snapshot(chunk_dir) == before)


def case_no_vitest_skips() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_tree(root, vitest_version=None)
        proc = run(root)
        check('no vitest exits 0', ok=proc.returncode == 0, detail=proc.stdout)
        check('no vitest says so', ok='vitest is not installed' in proc.stdout)


def case_unreadable_metadata_fails() -> None:
    """vitest is present but its version cannot be trusted: never report it as absent."""
    shapes = {
        'package.json is a directory': None,
        'package.json is missing': '',
        'package.json is malformed JSON': '{"name": "vitest", ',
        'package.json is not an object': '["vitest"]',
        'package.json has no version': '{"name": "vitest"}',
        'package.json version is not a string': '{"name": "vitest", "version": 5}',
    }
    for shape, raw in shapes.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunk_dir = make_tree(root)
            package_json = root / 'node_modules' / 'vitest' / 'package.json'
            package_json.unlink()
            if raw is None:
                package_json.mkdir()
            elif raw:
                package_json.write_text(raw, encoding='utf-8')
            before = snapshot(chunk_dir)
            proc = run(root)
            check(f'{shape} exits 1', ok=proc.returncode == 1, detail=f'rc={proc.returncode}')
            check(f'{shape} is not reported absent', ok='not installed' not in proc.stdout)
            check(f'{shape} writes nothing', ok=snapshot(chunk_dir) == before)


def case_dangling_vitest_link_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        modules = root / 'node_modules'
        modules.mkdir()
        (modules / 'vitest').symlink_to(root / 'gone')
        proc = run(root)
        check(
            'dangling vitest link exits 1', ok=proc.returncode == 1, detail=f'rc={proc.returncode}'
        )


def case_no_browser_pool_skips() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(root, chunks={'index.Node01.js': 'function createPool(ctx) {}\n'})
        before = snapshot(chunk_dir)
        proc = run(root)
        check('no browser pool exits 0', ok=proc.returncode == 0, detail=proc.stdout)
        check('no browser pool says so', ok='not applicable' in proc.stdout)
        check('no browser pool is left alone', ok=snapshot(chunk_dir) == before)


def case_renamed_chunk_is_found() -> None:
    """vitest 4.1 defines the pool in cli-api.*.js, 5.0 in index.*.js; neither name is a contract."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(root, chunks={'cli-api.Ren001.js': browser_pool(STOCK)})
        proc = run(root)
        text = (chunk_dir / 'cli-api.Ren001.js').read_text(encoding='utf-8')
        check('renamed chunk exits 0', ok=proc.returncode == 0, detail=proc.stdout)
        check('renamed chunk is patched', ok=text == browser_pool(PATCHED))


def case_pool_without_factory_fails_loudly() -> None:
    """A browser pool still ships but createBrowserPool is gone: the structure moved."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(
            root, chunks={'index.Gone01.js': 'class BrowserPool {}\nfunction makePools() {}\n'}
        )
        before = snapshot(chunk_dir)
        proc = run(root)
        check(
            'pool without factory exits 3', ok=proc.returncode == 3, detail=f'rc={proc.returncode}'
        )
        check('pool without factory says delete', ok='DELETE' in proc.stdout)
        check('pool without factory writes nothing', ok=snapshot(chunk_dir) == before)


def case_upstream_fix_fails_loudly() -> None:
    """The reason this script exists: upstream ships the fix, we go red, we delete it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(
            root,
            vitest_version='5.1.0',
            chunks={'index.Fix001.js': browser_pool(UPSTREAM_FIXED)},
        )
        before = snapshot(chunk_dir)
        proc = run(root)
        check('upstream fix exits 3', ok=proc.returncode == 3, detail=f'rc={proc.returncode}')
        check('upstream fix names the version', ok='5.1.0' in proc.stdout)
        check('upstream fix names the script', ok='patch-vitest-browser-bail.py' in proc.stdout)
        check('upstream fix says it was fixed', ok='appears to have fixed' in proc.stdout)
        check('upstream fix says delete', ok='DELETE' in proc.stdout)
        check('upstream fix writes nothing', ok=snapshot(chunk_dir) == before)


def case_exact_upstream_fix_fails_loudly() -> None:
    """Upstream ships byte-for-byte this script's insert: no stamp, so not ours to accept."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(
            root,
            vitest_version='5.1.0',
            chunks={'index.Fix002.js': browser_pool(PATCHED)},
        )
        before = snapshot(chunk_dir)
        proc = run(root)
        check('exact upstream fix exits 3', ok=proc.returncode == 3, detail=f'rc={proc.returncode}')
        check('exact upstream fix is not already patched', ok='already patched' not in proc.stdout)
        check('exact upstream fix says it was fixed', ok='appears to have fixed' in proc.stdout)
        check('exact upstream fix says delete', ok='DELETE' in proc.stdout)
        check('exact upstream fix writes nothing', ok=snapshot(chunk_dir) == before)


def case_stock_beside_fixed_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(
            root, chunks={'index.Mix001.js': browser_pool(PATCHED + '\n' + STOCK)}
        )
        before = snapshot(chunk_dir)
        proc = run(root)
        check('stock beside fixed exits 4', ok=proc.returncode == 4, detail=proc.stdout)
        check('stock beside fixed reports the counts', ok='fixed block x1' in proc.stdout)
        check('stock beside fixed writes nothing', ok=snapshot(chunk_dir) == before)


def case_missing_anchor_fails_loudly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        moved = ENSURE + '\t\t\tvitest.state.clearFiles(project, specs);'
        make_tree(root, chunks={'index.Mov001.js': browser_pool(moved)})
        proc = run(root)
        check('moved anchor exits 3', ok=proc.returncode == 3, detail=f'rc={proc.returncode}')
        check('moved anchor says it changed', ok='appears to have changed' in proc.stdout)
        check('moved anchor says delete', ok='DELETE' in proc.stdout)


def unrelated(body: str) -> str:
    return f'function runSomethingElse(vitest, project, files) {{\n{body}\n}}\n'


def case_stock_outside_factory_is_ignored() -> None:
    """The factory moved, but another function in the same chunk still carries the stock block."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        moved = ENSURE + '\t\t\tvitest.state.clearFiles(project, specs);'
        chunk_dir = make_tree(
            root, chunks={'index.Out001.js': unrelated(STOCK) + browser_pool(moved)}
        )
        before = snapshot(chunk_dir)
        proc = run(root)
        check(
            'stock outside a moved factory exits 3',
            ok=proc.returncode == 3,
            detail=f'rc={proc.returncode}',
        )
        check('stock outside a moved factory writes nothing', ok=snapshot(chunk_dir) == before)


def case_stock_outside_factory_refused() -> None:
    """The anchor must be unique across dist, not only inside the factory."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(
            root, chunks={'index.Both01.js': unrelated(STOCK) + browser_pool(STOCK)}
        )
        before = snapshot(chunk_dir)
        proc = run(root)
        check('stock inside and outside exits 4', ok=proc.returncode == 4, detail=proc.stdout)
        check('stock inside and outside reports the count', ok='anchor x2' in proc.stdout)
        check('stock inside and outside writes nothing', ok=snapshot(chunk_dir) == before)


def case_anchor_in_second_chunk_refused() -> None:
    """A marker-free chunk carrying the anchor still makes it ambiguous."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(
            root,
            chunks={
                'index.Pool01.js': browser_pool(STOCK),
                'other.Free01.js': unrelated(STOCK),
            },
        )
        before = snapshot(chunk_dir)
        proc = run(root)
        check('anchor in a second chunk exits 4', ok=proc.returncode == 4, detail=proc.stdout)
        check('anchor in a second chunk reports the count', ok='anchor x2' in proc.stdout)
        check('anchor in a second chunk writes nothing', ok=snapshot(chunk_dir) == before)


def case_marker_outside_declaration_refused() -> None:
    """Marker text in a string or comment must never define the patch region."""
    shapes = {
        'string only': "const name = 'function createBrowserPool';\nclass BrowserPool {}\n"
        + unrelated(STOCK),
        'comment beside the factory': '// see function createBrowserPool below\n'
        + browser_pool(STOCK),
    }
    for shape, text in shapes.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunk_dir = make_tree(root, chunks={'index.Str001.js': text})
            before = snapshot(chunk_dir)
            proc = run(root)
            check(f'marker in a {shape} exits 4', ok=proc.returncode == 4, detail=proc.stdout)
            check(f'marker in a {shape} names the cause', ok='as a declaration' in proc.stdout)
            check(f'marker in a {shape} writes nothing', ok=snapshot(chunk_dir) == before)


def case_unterminated_factory_fails_loudly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        open_ended = browser_pool(STOCK).removesuffix('}\n')
        chunk_dir = make_tree(root, chunks={'index.Open01.js': open_ended})
        before = snapshot(chunk_dir)
        proc = run(root)
        check(
            'unterminated factory exits 3', ok=proc.returncode == 3, detail=f'rc={proc.returncode}'
        )
        check('unterminated factory writes nothing', ok=snapshot(chunk_dir) == before)


def case_factory_defined_twice_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(
            root, chunks={'index.Twice1.js': browser_pool(STOCK) + browser_pool(STOCK)}
        )
        before = snapshot(chunk_dir)
        proc = run(root)
        check('factory twice is non-zero', ok=proc.returncode != 0, detail=proc.stdout)
        check('factory twice is not exit 3', ok=proc.returncode != 3)
        check('factory twice reports the count', ok='defined x2' in proc.stdout)
        check('factory twice writes nothing', ok=snapshot(chunk_dir) == before)


def case_duplicate_anchor_refused() -> None:
    """A chunk carrying the anchor twice changed shape; a blind insert was never measured."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(root, chunks={'index.Dup001.js': browser_pool(STOCK + '\n' + STOCK)})
        before = snapshot(chunk_dir)
        proc = run(root)
        check('duplicate anchor is non-zero', ok=proc.returncode != 0, detail=proc.stdout)
        check('duplicate anchor is not exit 3', ok=proc.returncode != 3)
        check('duplicate anchor reports the count', ok='anchor x2' in proc.stdout)
        check('duplicate anchor writes nothing', ok=snapshot(chunk_dir) == before)


def case_two_chunks_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chunk_dir = make_tree(
            root,
            chunks={
                'index.One001.js': browser_pool(STOCK),
                'index.Two002.js': browser_pool(STOCK),
            },
        )
        before = snapshot(chunk_dir)
        proc = run(root)
        check('two chunks is non-zero', ok=proc.returncode != 0, detail=proc.stdout)
        check('two chunks is not exit 3', ok=proc.returncode != 3)
        check(
            'two chunks names both',
            ok='dist/chunks/index.One001.js, dist/chunks/index.Two002.js' in proc.stdout,
        )
        check('two chunks writes nothing', ok=snapshot(chunk_dir) == before)


def main() -> int:
    if not PATCHER.is_file():
        print(f'FAIL  {PATCHER} is missing')
        return 1
    for case in (
        case_patches_stock_chunk,
        case_stale_stamp_is_not_ours,
        case_unreadable_stamp_fails,
        case_check_mode_writes_nothing,
        case_no_vitest_skips,
        case_unreadable_metadata_fails,
        case_dangling_vitest_link_fails,
        case_no_browser_pool_skips,
        case_renamed_chunk_is_found,
        case_pool_without_factory_fails_loudly,
        case_upstream_fix_fails_loudly,
        case_exact_upstream_fix_fails_loudly,
        case_stock_beside_fixed_refused,
        case_missing_anchor_fails_loudly,
        case_stock_outside_factory_is_ignored,
        case_stock_outside_factory_refused,
        case_anchor_in_second_chunk_refused,
        case_marker_outside_declaration_refused,
        case_unterminated_factory_fails_loudly,
        case_factory_defined_twice_refused,
        case_duplicate_anchor_refused,
        case_two_chunks_refused,
    ):
        print(f'{case.__name__}:')
        case()
    if FAILURES:
        print(f'\n{len(FAILURES)} failure(s): ' + ', '.join(FAILURES))
        return 1
    print('\nall checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
