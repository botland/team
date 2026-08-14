import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.gitutil import already_dirty_mutations, delta_paths, product_paths, verify_delta


class DeltaTests(unittest.TestCase):
    def test_delta(self):
        self.assertEqual(delta_paths(["a"], ["a", "tests/x.py"]), ["tests/x.py"])

    def test_verify_ok(self):
        ok, bad = verify_delta(
            ["tests/a.py", ".team/work/foo/design.md"],
            ["tests"],
            always_allowed=[".team/work"],
        )
        self.assertEqual(bad, [])
        self.assertEqual(len(ok), 2)

    def test_verify_violation(self):
        ok, bad = verify_delta(["src/a.py", "tests/a.py"], ["tests"])
        self.assertEqual(bad, ["src/a.py"])
        self.assertEqual(ok, ["tests/a.py"])

    def test_empty_root_advisory(self):
        ok, bad = verify_delta(["src/a.py"], [""])
        self.assertEqual(bad, [])
        self.assertEqual(ok, ["src/a.py"])

    def test_product_paths_drop_team_work(self):
        self.assertEqual(
            product_paths(["src/a.py", ".team/work/s/review.md", "tests/t.py"]),
            ["src/a.py", "tests/t.py"],
        )

    def test_already_dirty_is_run_start_not_hop_start(self):
        origin = {"NOTES": "h0"}
        before = {"NOTES": "h0", "src/a.py": "h1"}
        after = {"NOTES": "h0", "src/a.py": "h2"}
        self.assertEqual(
            already_dirty_mutations(
                ["src/a.py"], origin, before, after
            ),
            [],
        )
        self.assertEqual(
            already_dirty_mutations(
                ["NOTES"],
                origin,
                {"NOTES": "h0"},
                {"NOTES": "h1"},
            ),
            ["NOTES"],
        )

    def test_already_dirty_skips_work_root_and_cleaned_paths(self):
        origin = {"src/a.py": "h0", ".team/work/s/x": "w0"}
        self.assertEqual(
            already_dirty_mutations(
                [".team/work/s/x"],
                origin,
                {".team/work/s/x": "w0"},
                {".team/work/s/x": "w1"},
                exempt_roots=(".team/work",),
            ),
            [],
        )
        self.assertEqual(
            already_dirty_mutations(
                ["src/a.py"],
                origin,
                {},
                {"src/a.py": "h2"},
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
