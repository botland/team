import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.gitutil import delta_paths, verify_delta


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


if __name__ == "__main__":
    unittest.main()
