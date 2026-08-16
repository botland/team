import inspect
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
    CODING_RUNTIMES,
    OPTIONAL_PHASES,
    ROLES,
    WRITE_CAPABILITIES,
    apply_range_reviewer,
    collect_config_edits,
    expand_reviewer,
    is_model_runtime,
    load_config,
    may_write,
    normalize_effort,
    parse_simple_toml,
    resolve_config_key,
    role_accepts_runtime,
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


class SkipValidationTests(unittest.TestCase):
    """--skip named a phase the runner then ignored, and said nothing.

    `team --skip reviewer review` was accepted, printed nothing, and ran the
    reviewer. Every entry point (flag, TEAM_SKIP, [run] skip) resolves through
    the same optional-phase set, so a name outside it is an error once.
    """

    def test_optional_phases_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp), skip=list(OPTIONAL_PHASES))
            self.assertEqual(sorted(cfg.skip), sorted(OPTIONAL_PHASES))

    def test_aliases_still_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_config(Path(tmp), skip=["attack"]).skip, ["adversarial"])

    def test_a_phase_that_cannot_be_skipped_is_an_error(self):
        for name in ("reviewer", "implementer", "not-a-phase"):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(SystemExit) as ctx:
                        load_config(Path(tmp), skip=[name])
                    self.assertIn("cannot skip", str(ctx.exception))

    def test_the_env_var_and_the_config_file_use_the_same_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            path = repo / ".team" / "config.toml"
            path.parent.mkdir(parents=True)
            path.write_text('[run]\nskip = ["reviewer"]\n', encoding="utf-8")
            with self.assertRaises(SystemExit):
                load_config(repo)
            path.write_text("[run]\nskip = []\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"TEAM_SKIP": "reviewer"}):
                with self.assertRaises(SystemExit):
                    load_config(repo)

    def test_the_runner_honours_exactly_this_set(self):
        from team.pipeline import Pipeline

        source = inspect.getsource(Pipeline._skip_reason)
        self.assertIn("OPTIONAL_PHASES", source)


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

    def test_only_write_capabilities_may_write(self):
        used = {
            spec["capability"]
            for spec in ROLES.values()
            if may_write(spec["capability"])
        }
        self.assertEqual(used, set(WRITE_CAPABILITIES))
        self.assertFalse(may_write("read-only"))
        self.assertFalse(may_write("execute"))
        self.assertFalse(may_write("future-inspect"))
        self.assertFalse(may_write(""))
        self.assertTrue(may_write("write-tests"))
        self.assertTrue(may_write("write-code"))

    def test_coding_roles_accept_every_shipped_adapter(self):
        for role, spec in ROLES.items():
            allowed = set(spec["runtimes"]) - {"both", "host"}
            self.assertEqual(
                allowed,
                set(CODING_RUNTIMES),
                "%s must accept every shipped coding runtime, got %s"
                % (role, spec["runtimes"]),
            )
            for runtime in CODING_RUNTIMES:
                self.assertTrue(role_accepts_runtime(role, runtime), (role, runtime))

    def test_expand_reviewer_is_the_both_list(self):
        self.assertEqual(expand_reviewer("both"), list(CODING_RUNTIMES))
        self.assertEqual(expand_reviewer("claude"), ["claude"])
        self.assertEqual(expand_reviewer("grok"), ["grok"])
        self.assertEqual(expand_reviewer(""), [])

    def test_host_is_not_a_model_runtime(self):
        self.assertFalse(is_model_runtime("host"))
        self.assertFalse(is_model_runtime("both"))
        self.assertTrue(is_model_runtime("claude"))
        self.assertTrue(is_model_runtime("grok"))
        self.assertTrue(is_model_runtime("fake"))

    def test_registered_adapter_is_assignable_like_the_shipped_pair(self):
        from team.runners import FakeRuntime, register, unregister

        register("codex", FakeRuntime)
        try:
            self.assertTrue(is_model_runtime("codex"))
            self.assertTrue(role_accepts_runtime("architect", "codex"))
            self.assertTrue(role_accepts_runtime("implementer", "codex"))
            self.assertTrue(role_accepts_runtime("reviewer", "codex"))
            self.assertFalse(role_accepts_runtime("tester", "both"))
            with tempfile.TemporaryDirectory() as tmp:
                cfg = load_config(
                    Path(tmp),
                    assign=["architect=codex", "implementer=codex", "reviewer=codex"],
                )
                self.assertEqual(cfg.assignment("architect"), "codex")
                self.assertEqual(cfg.assignment("implementer"), "codex")
                self.assertEqual(cfg.assignment("reviewer"), "codex")
                self.assertEqual(expand_reviewer(cfg.assignment("reviewer")), ["codex"])
                cfg = load_config(Path(tmp), assign=["all=codex"])
                self.assertEqual(cfg.assignment("architect"), "codex")
                self.assertEqual(cfg.assignment("implementer"), "codex")
                self.assertEqual(cfg.assignment("tester"), "codex")
                self.assertEqual(cfg.assignment("reviewer"), "codex")
        finally:
            unregister("codex")

    def test_unregistered_adapter_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                load_config(Path(tmp), assign=["architect=codex"])
        self.assertFalse(role_accepts_runtime("architect", "codex"))

    def test_pipeline_does_not_name_the_shipped_pair(self):
        src = (
            Path(__file__).resolve().parents[1] / "src" / "team" / "pipeline.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn('["claude", "grok"]', src)
        self.assertNotIn("['claude', 'grok']", src)
        self.assertIn("expand_reviewer", src)

    def test_assign_rejects_grokxhigh_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                load_config(Path(tmp), assign=["architect=grokxhigh"])

    def test_effort_from_toml_and_cli_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team").mkdir()
            (repo / ".team" / "config.toml").write_text(
                '[effort]\narchitect = "xhigh"\n',
                encoding="utf-8",
            )
            # Legacy names still load; they are stored as neutral levels.
            cfg = load_config(repo)
            self.assertEqual(cfg.effort["architect"], 4)
            self.assertEqual(cfg.effort_for("architect"), 4)
            self.assertEqual(cfg.effort_for("critic"), 3)
            self.assertIsNone(cfg.effort_for("implementer"))
            cfg = load_config(repo, effort=["implementer=2", "all=3"])
            self.assertEqual(cfg.effort_for("architect"), 3)
            self.assertEqual(cfg.effort_for("implementer"), 3)
            cfg = load_config(repo, effort=["all=3", "implementer=2"])
            self.assertEqual(cfg.effort_for("architect"), 3)
            self.assertEqual(cfg.effort_for("implementer"), 2)
            self.assertEqual(cfg.roles["architect"], "claude")
            # Names and integers are the same setting, not two settings.
            self.assertEqual(
                load_config(repo, effort=["all=high"]).effort_for("architect"),
                load_config(repo, effort=["all=3"]).effort_for("architect"),
            )

    def test_effort_integer_scale_covers_both_ends(self):
        """0 is a real level, not "unset", and 5 is accepted even where a
        runtime has no fifth rung -- runners snap it, config keeps it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = load_config(repo, effort=["all=0"])
            self.assertEqual(cfg.effort_for("architect"), 0)
            self.assertIsNotNone(cfg.effort_for("implementer"))
            cfg = load_config(repo, effort=["all=5"])
            self.assertEqual(cfg.effort_for("architect"), 5)
            self.assertEqual(cfg.effort_for("architect"), normalize_effort("max"))

    def test_effort_rejects_unknown_level_and_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                load_config(Path(tmp), effort=["architect=ludicrous"])
            with self.assertRaises(SystemExit):
                load_config(Path(tmp), effort=["not-a-role=xhigh"])
            with self.assertRaises(SystemExit):
                load_config(Path(tmp), effort=["xhigh"])
            # Out of the neutral range, on both ends.
            with self.assertRaises(SystemExit):
                load_config(Path(tmp), effort=["architect=6"])
            with self.assertRaises(SystemExit):
                load_config(Path(tmp), effort=["architect=-1"])
            # "max" is a real rung now (Claude has one); it must not be rejected.
            self.assertEqual(
                load_config(Path(tmp), effort=["architect=max"]).effort_for("architect"),
                5,
            )

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
            cfg = load_config(Path(tmp))
            apply_range_reviewer(cfg, pr=False, reviewer="both")
            self.assertEqual(cfg.roles["reviewer"], "both")
            cfg = load_config(Path(tmp), assign=["reviewer=both"])
            apply_range_reviewer(cfg, pr=False)
            self.assertEqual(cfg.roles["reviewer"], "both")

    def test_range_reviewer_accepts_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team").mkdir()
            (repo / ".team" / "config.toml").write_text(
                '[review]\nrange_reviewer = "both"\n', encoding="utf-8"
            )
            cfg = load_config(repo)
            self.assertEqual(cfg.range_reviewer, "both")
            apply_range_reviewer(cfg, pr=False)
            self.assertEqual(cfg.roles["reviewer"], "both")

    def test_range_reviewer_rejects_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".team").mkdir()
            (repo / ".team" / "config.toml").write_text(
                '[review]\nrange_reviewer = "host"\n', encoding="utf-8"
            )
            with self.assertRaises(SystemExit):
                load_config(repo)

    def test_assign_all_both_sets_range_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp), assign=["all=both"])
            self.assertEqual(cfg.roles["reviewer"], "both")
            self.assertEqual(cfg.range_reviewer, "both")
            self.assertEqual(cfg.roles["implementer"], "grok")


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

    def test_collect_range_reviewer_both(self):
        updates, deletes = collect_config_edits(range_reviewer="both")
        self.assertEqual(deletes, [])
        self.assertEqual(updates, [("review", "range_reviewer", "both")])
        updates, deletes = collect_config_edits(assign=["all=both"])
        self.assertIn(("review", "range_reviewer", "both"), updates)
        with self.assertRaises(SystemExit):
            collect_config_edits(range_reviewer="host")

    def test_collect_unset_role_deletes(self):
        updates, deletes = collect_config_edits(unsets=["architect"])
        self.assertEqual(updates, [])
        self.assertEqual(deletes, [("roles", "architect")])

    def test_effort_config_key_and_collect(self):
        self.assertEqual(
            resolve_config_key("effort.architect"),
            ("effort", "architect", "effort"),
        )
        updates, deletes = collect_config_edits(effort=["architect=xhigh"])
        self.assertEqual(deletes, [])
        self.assertEqual(updates, [("effort", "architect", 4)])
        updates, deletes = collect_config_edits(effort=["all=low"])
        self.assertEqual(deletes, [])
        self.assertTrue(all(section == "effort" for section, _key, _val in updates))
        self.assertEqual(len(updates), len(ROLES))
        updates, deletes = collect_config_edits(unsets=["effort.reviewer"])
        self.assertEqual(updates, [])
        self.assertEqual(deletes, [("effort", "reviewer")])
        with self.assertRaises(SystemExit):
            collect_config_edits(effort=["architect=ludicrous"])
        with self.assertRaises(SystemExit):
            collect_config_edits(effort=["architect=6"])


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

    def test_written_values_read_back_unchanged(self):
        """The seam: the writer is hand-rolled, the reader is tomllib.

        Fails if either side moves alone. A ``#`` inside a quoted value was
        the instance -- team config wrote a test_command the loader then
        truncated, and testhost ran the truncated string with shell=True.
        """
        values = [
            'pytest -k "not slow" -m "a#b"',
            "# leading hash",
            "trailing hash #",
            "back\\slash",
            'quote " and \\" escaped',
            "tab\there",
            "line\nbreak",
            "unicode café ☕",
            "equals = sign",
            "bracket [not a section]",
            "",
            "   spaced   ",
        ]
        for value in values:
            with self.subTest(value=value):
                text = update_simple_toml("", [("paths", "test_command", value)])
                self.assertEqual(
                    parse_simple_toml(text)["paths"]["test_command"], value
                )
        text = update_simple_toml(
            "",
            [
                ("run", "skip", ["critic", "a#b", 'q"uote']),
                ("run", "phase_timeout", 900),
                ("default", "top_level", True),
            ],
        )
        data = parse_simple_toml(text)
        self.assertEqual(data["run"]["skip"], ["critic", "a#b", 'q"uote'])
        self.assertEqual(data["run"]["phase_timeout"], 900)
        self.assertEqual(data["default"]["top_level"], True)

    def test_config_command_round_trips_a_hash_in_the_test_command(self):
        cmd = 'pytest -k "not slow" -m "a#b"'
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            rc = main(["--repo", str(repo), "config", "--test-command", cmd])
            self.assertEqual(rc, 0)
            cfg = load_config(repo)
            self.assertEqual(cfg.test_command, cmd)

    def test_unreadable_config_file_stops_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            path = repo / ".team" / "config.toml"
            path.parent.mkdir(parents=True)
            path.write_text('[paths]\ntest_command = "unterminated\n', encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                load_config(repo)
            self.assertIn("config.toml", str(ctx.exception))

    def test_seed_example_has_path_keys(self):
        text = seed_config_text()
        self.assertIn("code_root", text)
        self.assertIn("[roles]", text)
        self.assertIn("[effort]", text)


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

    def test_init_writes_the_seed_and_a_missing_example_is_an_error_line(self):
        rc, out, _err = self._run(["--repo", str(self.repo), "init"])
        self.assertEqual(rc, 0)
        dest = self.repo / ".team" / "config.toml"
        self.assertIn("[roles]", dest.read_text(encoding="utf-8"))
        self.assertIn(str(dest), out)
        dest.unlink()
        (self.engine / "config.example.toml").unlink()
        rc, _out, err = self._run(["--repo", str(self.repo), "init"])
        self.assertEqual(rc, 1)
        self.assertIn("config.example.toml", err)
        self.assertFalse(dest.exists())

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

    def test_range_reviewer_flag_both(self):
        rc, out, err = self._run(
            [
                "--repo",
                str(self.repo),
                "config",
                "--range-reviewer",
                "both",
            ]
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("set review.range_reviewer", out)
        cfg = load_config(self.repo)
        self.assertEqual(cfg.range_reviewer, "both")

    def test_writes_effort_and_load_config_sees_it(self):
        rc, out, err = self._run(
            [
                "--repo",
                str(self.repo),
                "config",
                "--effort",
                "architect=xhigh",
                "effort.implementer=low",
            ]
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("set effort.architect", out)
        self.assertIn("set effort.implementer", out)
        dest = self.repo / ".team" / "config.toml"
        body = dest.read_text(encoding="utf-8")
        self.assertIn("[effort]", body)
        # Written as the neutral level, whichever spelling was typed.
        self.assertIn("architect = 4", body)
        self.assertIn("implementer = 1", body)
        cfg = load_config(self.repo)
        self.assertEqual(cfg.effort_for("architect"), 4)
        self.assertEqual(cfg.effort_for("implementer"), 1)
        rc, out, err = self._run(
            ["--repo", str(self.repo), "config", "--unset", "effort.architect"]
        )
        self.assertEqual(rc, 0, err)
        parsed = parse_simple_toml(dest.read_text(encoding="utf-8"))
        self.assertNotIn("architect", parsed.get("effort") or {})
        self.assertEqual((parsed.get("effort") or {}).get("implementer"), 1)
        cfg = load_config(self.repo)
        self.assertEqual(cfg.effort_for("architect"), 3)
        self.assertEqual(cfg.effort_for("implementer"), 1)

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
