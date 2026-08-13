import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.cli import main
from team.config import (
    ROLES,
    apply_range_reviewer,
    collect_config_edits,
    load_config,
    parse_simple_toml,
    resolve_config_key,
    seed_config_text,
    update_simple_toml,
)


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


class ConfigKeyTests(unittest.TestCase):
    def test_aliases_and_roles(self):
        self.assertEqual(resolve_config_key("code-root"), ("paths", "code_root", "str"))
        self.assertEqual(
            resolve_config_key("paths.test_root"), ("paths", "test_root", "str")
        )
        self.assertEqual(resolve_config_key("tdd-design"), ("roles", "tdd-design", "role"))
        self.assertEqual(
            resolve_config_key("roles.test_writer"), ("roles", "test-writer", "role")
        )
        with self.assertRaises(SystemExit):
            resolve_config_key("not-a-key")

    def test_collect_later_pairs_win(self):
        updates, deletes = collect_config_edits(
            code_root="first",
            pairs=["code_root=second"],
        )
        self.assertEqual(deletes, [])
        self.assertEqual(updates, [("paths", "code_root", "second")])

    def test_collect_rejects_bad_role(self):
        with self.assertRaises(SystemExit):
            collect_config_edits(assign=["tester=both"])

    def test_collect_unset_role_deletes(self):
        updates, deletes = collect_config_edits(unsets=["architect"])
        self.assertEqual(updates, [])
        self.assertEqual(deletes, [("roles", "architect")])


class TomlUpdateTests(unittest.TestCase):
    def test_preserves_comments_and_updates_in_place(self):
        src = (
            "# header\n"
            "[paths]\n"
            "# Leave empty to let the architect discover roots.\n"
            'code_root = ""  # keep me\n'
            'test_root = ""\n'
        )
        out = update_simple_toml(
            src,
            [
                ("paths", "code_root", "inferedge-phase1/controller"),
                ("paths", "test_root", "inferedge-phase1/tests"),
            ],
        )
        self.assertIn("# header", out)
        self.assertIn("# Leave empty to let the architect discover roots.", out)
        self.assertIn(
            'code_root = "inferedge-phase1/controller"  # keep me', out
        )
        self.assertIn('test_root = "inferedge-phase1/tests"', out)

    def test_appends_missing_section(self):
        out = update_simple_toml("[roles]\narchitect = \"claude\"\n", [("run", "skip", ["critic"])])
        self.assertIn("[roles]", out)
        self.assertIn('architect = "claude"', out)
        self.assertIn("[run]", out)
        self.assertIn('skip = ["critic"]', out)

    def test_seed_example_has_path_keys(self):
        text = seed_config_text()
        self.assertIn("code_root", text)
        self.assertIn("[roles]", text)


class ConfigCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.engine = root / "engine"
        self.repo.mkdir()
        self.engine.mkdir()
        example = Path(__file__).resolve().parents[1] / "config.example.toml"
        (self.engine / "config.example.toml").write_text(
            example.read_text(encoding="utf-8"), encoding="utf-8"
        )
        self._old_home = os.environ.get("TEAM_HOME")
        os.environ["TEAM_HOME"] = str(self.engine)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("TEAM_HOME", None)
        else:
            os.environ["TEAM_HOME"] = self._old_home
        self.tmp.cleanup()

    def _run(self, argv):
        buf = StringIO()
        err = StringIO()
        with mock.patch("sys.stdout", buf), mock.patch("sys.stderr", err):
            rc = main(argv)
        return rc, buf.getvalue(), err.getvalue()

    def test_show_does_not_create_file(self):
        rc, out, err = self._run(["--repo", str(self.repo), "config"])
        self.assertEqual(rc, 0, err)
        self.assertIn("exists: no", out)
        self.assertIn("(unset)", out)
        self.assertFalse((self.repo / ".team" / "config.toml").exists())

    def test_writes_roots_and_load_config_sees_them(self):
        rc, out, err = self._run(
            [
                "--repo",
                str(self.repo),
                "config",
                "--code-root",
                "inferedge-phase1/controller",
                "--test-root",
                "inferedge-phase1/tests",
            ]
        )
        self.assertEqual(rc, 0, err)
        dest = self.repo / ".team" / "config.toml"
        self.assertTrue(dest.is_file())
        self.assertTrue((self.repo / ".team" / ".gitignore").is_file())
        self.assertIn("set paths.code_root", out)
        self.assertIn("set paths.test_root", out)
        body = dest.read_text(encoding="utf-8")
        self.assertIn("# Leave empty to let the architect discover roots.", body)
        self.assertIn('code_root = "inferedge-phase1/controller"', body)
        self.assertIn('test_root = "inferedge-phase1/tests"', body)
        cfg = load_config(self.repo)
        self.assertEqual(cfg.code_root, "inferedge-phase1/controller")
        self.assertEqual(cfg.test_root, "inferedge-phase1/tests")

    def test_global_flags_before_command_persist(self):
        rc, _out, err = self._run(
            [
                "--repo",
                str(self.repo),
                "--code-root",
                "src",
                "--test-root",
                "tests",
                "config",
            ]
        )
        self.assertEqual(rc, 0, err)
        cfg = load_config(self.repo)
        self.assertEqual(cfg.code_root, "src")
        self.assertEqual(cfg.test_root, "tests")

    def test_pairs_and_assign_and_skip(self):
        rc, out, err = self._run(
            [
                "--repo",
                str(self.repo),
                "config",
                "--assign",
                "architect=grok",
                "--skip",
                "critic,guardian",
                "test_command=make test",
                "phase_timeout=60",
                "range_reviewer=claude",
            ]
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("set roles.architect", out)
        cfg = load_config(self.repo)
        self.assertEqual(cfg.roles["architect"], "grok")
        self.assertEqual(cfg.skip, ["critic", "guardian"])
        self.assertEqual(cfg.test_command, "make test")
        self.assertEqual(cfg.phase_timeout, 60)
        self.assertEqual(cfg.range_reviewer, "claude")

    def test_unset_path_and_role(self):
        dest = self.repo / ".team"
        dest.mkdir()
        (dest / "config.toml").write_text(
            '[paths]\ncode_root = "src"\n[roles]\narchitect = "grok"\n',
            encoding="utf-8",
        )
        rc, out, err = self._run(
            [
                "--repo",
                str(self.repo),
                "config",
                "--unset",
                "code_root",
                "--unset",
                "architect",
            ]
        )
        self.assertEqual(rc, 0, err)
        body = (dest / "config.toml").read_text(encoding="utf-8")
        self.assertIn('code_root = ""', body)
        self.assertNotIn("architect", body)
        self.assertIn("unset roles.architect", out)
        cfg = load_config(self.repo)
        self.assertEqual(cfg.code_root, "")
        self.assertEqual(cfg.roles["architect"], "claude")

    def test_invalid_role_leaves_file_untouched(self):
        rc, _out, err = self._run(
            ["--repo", str(self.repo), "config", "--assign", "tester=both"]
        )
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)
        self.assertFalse((self.repo / ".team" / "config.toml").exists())

    def test_does_not_write_engine_config(self):
        engine = Path(os.environ["TEAM_HOME"]) / "config.toml"
        before = engine.read_text(encoding="utf-8") if engine.is_file() else None
        rc, _out, err = self._run(
            ["--repo", str(self.repo), "config", "--code-root", "src"]
        )
        self.assertEqual(rc, 0, err)
        after = engine.read_text(encoding="utf-8") if engine.is_file() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
