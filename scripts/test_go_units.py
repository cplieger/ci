"""scripts/go-units.sh: the Go modules a weekly fan-out treats as units.

The weekly fuzz and bench runs once probed only a root go.mod, so a repository
made of nested modules (cplieger/go) got no run at all, and a nested module
beside a root one was never fuzzed. Each case builds a tree and checks the
units, their slugs and the per-module counts.
"""

from __future__ import annotations

import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / 'go-units.sh'
FUZZ = '^func Fuzz'


def run(tree: pathlib.Path, *args: str) -> tuple[int, list[list[str]], str]:
    out = subprocess.run(
        ['bash', str(SCRIPT), str(tree), *args], capture_output=True, text=True, check=False
    )
    rows = [line.split('\t') for line in out.stdout.splitlines()]
    return out.returncode, rows, out.stderr


class GoUnits(unittest.TestCase):
    def tree(self, files: dict[str, str]) -> pathlib.Path:
        root = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        for rel, body in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        return root

    def test_a_repository_of_nested_modules_lists_each_with_its_own_count(self):
        tree = self.tree(
            {
                'a/go.mod': 'module a\n',
                'a/a.go': 'package a\n',
                'a/a_test.go': 'func FuzzA(f *testing.F) {}\nfunc FuzzB(f *testing.F) {}\n',
                'b/c/go.mod': 'module c\n',
                'b/c/c.go': 'package c\n',
                'b/c/x/c_test.go': 'func FuzzC(f *testing.F) {}\n',
            }
        )
        rc, rows, _ = run(tree, FUZZ)
        self.assertEqual(rc, 0)
        self.assertEqual(rows, [['a', 'a', '2'], ['b/c', 'b_c', '1']])

    def test_the_root_comes_first_and_does_not_count_a_nested_module_s_tests(self):
        tree = self.tree(
            {
                'go.mod': 'module r\n',
                'r.go': 'package r\n',
                'r_test.go': 'func FuzzR(f *testing.F) {}\n',
                'sub/go.mod': 'module s\n',
                'sub/s.go': 'package s\n',
                'sub/s_test.go': 'func FuzzS1(f *testing.F) {}\nfunc FuzzS2(f *testing.F) {}\n',
                'other/o_test.go': 'func FuzzO(f *testing.F) {}\n',
            }
        )
        rc, rows, _ = run(tree, FUZZ)
        self.assertEqual(rc, 0)
        self.assertEqual(rows, [['.', 'root', '2'], ['sub', 'sub', '2']])

    def test_without_a_regex_every_count_is_zero(self):
        tree = self.tree({'go.mod': 'module r\n', 'r.go': '', 'r_test.go': 'func FuzzR() {}\n'})
        self.assertEqual(run(tree)[1], [['.', 'root', '0']])

    def test_a_go_mod_with_no_go_file_or_in_a_pruned_dir_is_not_a_module(self):
        tree = self.tree(
            {
                'web/go.mod': 'module sentinel\n',
                'web/index.ts': '',
                'node_modules/x/go.mod': 'module x\n',
                'node_modules/x/x.go': '',
                'vendor/y/go.mod': 'module y\n',
                'vendor/y/y.go': '',
                'pkg/testdata/z/go.mod': 'module z\n',
                'pkg/testdata/z/z.go': '',
                'm/go.mod': 'module m\n',
                'm/deep/m.go': '',
            }
        )
        self.assertEqual(run(tree, FUZZ)[1], [['m', 'm', '0']])

    def test_tests_under_testdata_or_vendor_do_not_count(self):
        tree = self.tree(
            {
                'go.mod': 'module r\n',
                'r.go': '',
                'testdata/t_test.go': 'func FuzzT() {}\n',
                'vendor/v/v_test.go': 'func FuzzV() {}\n',
            }
        )
        self.assertEqual(run(tree, FUZZ)[1], [['.', 'root', '0']])

    def test_an_unsafe_dir_or_a_taken_slug_is_skipped_with_a_warning(self):
        tree = self.tree(
            {
                'a b/go.mod': 'module ab\n',
                'a b/x.go': '',
                'p/q/go.mod': 'module pq\n',
                'p/q/x.go': '',
                'p_q/go.mod': 'module p_q\n',
                'p_q/x.go': '',
            }
        )
        rc, rows, err = run(tree)
        self.assertEqual(rc, 0)
        self.assertEqual(rows, [['p/q', 'p_q', '0']])
        self.assertIn("skipped module dir 'a b'", err)
        self.assertIn(
            "skipped module dir 'p_q', because module dir 'p/q' already has slug 'p_q'", err
        )

    def test_a_tree_with_no_module_prints_nothing(self):
        tree = self.tree({'README.md': ''})
        self.assertEqual(run(tree, FUZZ), (0, [], ''))

    def test_a_missing_tree_or_extra_argument_is_a_usage_error(self):
        self.assertEqual(run(pathlib.Path('/nonexistent/tree'))[0], 2)
        tree = self.tree({'go.mod': ''})
        self.assertEqual(run(tree, FUZZ, 'extra')[0], 2)


if __name__ == '__main__':
    unittest.main()
