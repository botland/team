import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.config import load_config, parse_simple_toml


class TomlTests(unittest.TestCase):
    def test_parse(self):
        data = parse_simple_toml(
            """
            [roles]
            architect = "grok"
            implementer = "claude"
            [paths]
            test_root = "tests"
            [run]
            skip = ["critic", "guardian"]
            """
        )
        self.assertEqual(data["roles"]["architect"], "grok")
        self.assertEqual(data["paths"]["test_root"], "tests")
        self.assertEqual(data["run"]["skip"], ["critic", "guardian"])


class LoadConfigTests(unittest.TestCase):
    def test_assign_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp), assign=["architect=grok", "implementer=claude"])
            self.assertEqual(cfg.roles["architect"], "grok")
            self.assertEqual(cfg.roles["implementer"], "claude")
            self.assertEqual(cfg.roles["test-writer"], "grok")

    def test_bad_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                load_config(Path(tmp), assign=["tester=both"])

    def test_project_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team").mkdir()
            (repo / ".team" / "config.toml").write_text(
                '[roles]\narchitect = "grok"\n', encoding="utf-8"
            )
            cfg = load_config(repo)
            self.assertEqual(cfg.roles["architect"], "grok")


if __name__ == "__main__":
    unittest.main()
