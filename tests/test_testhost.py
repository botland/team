import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.testhost import compare, parse_failing_names


class CompareTests(unittest.TestCase):
    def test_regression(self):
        base = {"status": "PASS", "failing": []}
        final = {"status": "FAIL", "failing": ["tests/a.py"]}
        self.assertEqual(compare(base, final)["verdict"], "REGRESSION")

    def test_broken_baseline(self):
        base = {"status": "FAIL", "failing": ["old"]}
        final = {"status": "FAIL", "failing": ["old"]}
        self.assertEqual(compare(base, final)["verdict"], "BROKEN_BASELINE")

    def test_unverified(self):
        base = {"status": "UNVERIFIED", "failing": []}
        final = {"status": "UNVERIFIED", "failing": []}
        self.assertEqual(compare(base, final)["verdict"], "UNVERIFIED")

    def test_parse_pytest(self):
        log = "FAILED tests/test_a.py::test_x\nFAILED tests/test_b.py::test_y\n"
        self.assertEqual(
            parse_failing_names(log),
            ["tests/test_a.py::test_x", "tests/test_b.py::test_y"],
        )


if __name__ == "__main__":
    unittest.main()
