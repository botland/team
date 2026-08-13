import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.config import ROLES, apply_range_reviewer, load_config, parse_simple_toml


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

    def test_assign_all_grok(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp), assign=["all=grok"])
            for role, spec in ROLES.items():
                if "grok" in spec["runtimes"]:
                    self.assertEqual(cfg.roles[role], "grok", role)
                    self.assertIn(role, cfg.role_overrides)
            self.assertEqual(cfg.range_reviewer, "grok")

    def test_assign_all_then_one_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(
                Path(tmp), assign=["all=grok", "architect=claude", "tester=host"]
            )
            self.assertEqual(cfg.roles["implementer"], "grok")
            self.assertEqual(cfg.roles["guardian"], "grok")
            self.assertEqual(cfg.roles["architect"], "claude")
            self.assertEqual(cfg.roles["tester"], "host")

    def test_assign_all_host_only_tester(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp), assign=["all=host"])
            self.assertEqual(cfg.roles["tester"], "host")
            self.assertEqual(cfg.roles["implementer"], "grok")
            self.assertNotIn("implementer", cfg.role_overrides)

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

    def test_range_reviewer_from_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team").mkdir()
            (repo / ".team" / "config.toml").write_text(
                '[review]\nrange_reviewer = "claude"\n', encoding="utf-8"
            )
            cfg = load_config(repo)
            self.assertEqual(cfg.range_reviewer, "claude")

    def test_apply_range_reviewer_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp), assign=["reviewer=claude"])
            # --assign on a PR is honored
            apply_range_reviewer(cfg, pr=True)
            self.assertEqual(cfg.roles["reviewer"], "claude")
            cfg = load_config(Path(tmp))
            cfg.roles["reviewer"] = "claude"
            apply_range_reviewer(cfg, pr=True)
            self.assertEqual(cfg.roles["reviewer"], "both")
            cfg = load_config(Path(tmp))
            apply_range_reviewer(cfg, pr=False)
            self.assertEqual(cfg.roles["reviewer"], "grok")
            cfg = load_config(Path(tmp))
            apply_range_reviewer(cfg, pr=False, reviewer="claude")
            self.assertEqual(cfg.roles["reviewer"], "claude")

    def test_range_reviewer_rejects_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team").mkdir()
            (repo / ".team" / "config.toml").write_text(
                '[review]\nrange_reviewer = "both"\n', encoding="utf-8"
            )
            with self.assertRaises(SystemExit):
                load_config(repo)


if __name__ == "__main__":
    unittest.main()
