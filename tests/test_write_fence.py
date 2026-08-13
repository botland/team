"""Write fence: content-and-commit aware; empty roots fail closed.

Covers already-dirty invisibility and empty code_root/test_root fail-open.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.cli import main
from team.config import load_config
from team.gitutil import snapshot
from team.pipeline import PipelineError, start_feature
from team.state import State
from team.util import dump_json, under_root
from tests.support.hostile import (
    HostileRuntime,
    commit,
    delete,
    emit,
    register_runtime,
    write,
)
from tests.support.repo import init_repo


def _changed_paths():
    for mod_name, attr in (("team.gitutil", "changed_paths"), ("team.fence", "changed_paths")):
        try:
            mod = __import__(mod_name, fromlist=[attr])
        except ImportError:
            continue
        fn = getattr(mod, attr, None)
        if callable(fn):
            return fn
    return None


class UnderRootDotTests(unittest.TestCase):
    def test_dot_root_contains_every_path(self):
        self.assertTrue(under_root("src/x.py", "."))
        self.assertTrue(under_root("foo.py", "."))
        self.assertTrue(under_root("tests/a.py", "./"))


class SnapshotIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_records_content_identity_for_dirty_paths(self):
        target = self.repo / "README"
        target.write_text("dirty-a\n", encoding="utf-8")
        before = snapshot(self.repo)
        entries = before.get("entries") or before.get("content")
        self.assertTrue(isinstance(entries, dict), "snapshot must record per-path content ids")
        self.assertIn("README", entries)
        target.write_text("dirty-b\n", encoding="utf-8")
        after = snapshot(self.repo)
        after_entries = after.get("entries") or after.get("content")
        self.assertNotEqual(entries["README"], after_entries["README"])
        fn = _changed_paths()
        self.assertIsNotNone(fn, "changed_paths is the fence authority")
        self.assertIn("README", fn(self.repo, before, after))

    def test_changed_paths_includes_a_commit_that_clears_porcelain(self):
        from tests.support.repo import git

        before = snapshot(self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "x.py").write_text("x\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "hostile")
        after = snapshot(self.repo)
        fn = _changed_paths()
        self.assertIsNotNone(fn, "changed_paths is the fence authority")
        self.assertIn("src/x.py", fn(self.repo, before, after))


class WriteFenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "keep.py").write_text("keep\n", encoding="utf-8")
        (self.repo / "tests" / "test_a.py").write_text("ok\n", encoding="utf-8")
        from tests.support.repo import git

        git(self.repo, "add", "--", "src/keep.py", "tests/test_a.py")
        git(self.repo, "commit", "-m", "seed roots")

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, code_root="src", test_root="tests"):
        return load_config(
            self.repo,
            fake=True,
            force=True,
            code_root=code_root,
            test_root=test_root,
        )

    def _pipe(self, code_root="src", test_root="tests", slug="fence"):
        return start_feature(self._cfg(code_root, test_root), "fence brief", slug)

    def _write_summary(self, paths):
        return emit({"summary": "hostile", "paths_touched": list(paths)})

    def test_already_dirty_edit_is_a_violation(self):
        target = self.repo / "src" / "x.py"
        target.write_text("orig\n", encoding="utf-8")
        pipe = self._pipe()
        hostile = HostileRuntime(
            [
                write("src/x.py", "more\n", append=True),
                self._write_summary(["src/x.py"]),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        self.assertIn("src/x.py", str(ctx.exception))
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertIn("violations:", verify)
        self.assertIn("src/x.py", verify)

    def test_delete_under_other_root_clean_is_a_violation(self):
        pipe = self._pipe()
        hostile = HostileRuntime(
            [delete("tests/test_a.py"), self._write_summary(["tests/test_a.py"])],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        self.assertIn("tests/test_a.py", str(ctx.exception))
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertIn("violations:", verify)
        self.assertIn("tests/test_a.py", verify)

    def test_delete_under_other_root_already_dirty_is_a_violation(self):
        (self.repo / "tests" / "test_a.py").write_text("dirty\n", encoding="utf-8")
        pipe = self._pipe()
        hostile = HostileRuntime(
            [delete("tests/test_a.py"), self._write_summary(["tests/test_a.py"])],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        self.assertIn("tests/test_a.py", str(ctx.exception))
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertIn("violations:", verify)
        self.assertIn("tests/test_a.py", verify)

    def test_commit_of_out_of_role_writes_is_a_violation(self):
        pipe = self._pipe()
        hostile = HostileRuntime(
            [
                write("src/x.py", "secret\n"),
                commit("hostile write", ["src/x.py"]),
                self._write_summary(["src/x.py"]),
            ],
            phases=("test-writer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_test_writer()
        self.assertIn("src/x.py", str(ctx.exception))
        verify = (pipe.work / "git" / "verify-test-writer.md").read_text(encoding="utf-8")
        self.assertIn("violations:", verify)
        self.assertIn("src/x.py", verify)

    def test_in_role_commit_is_not_silently_green(self):
        pipe = self._pipe()
        hostile = HostileRuntime(
            [
                write("tests/test_ok.py", "def test_ok():\n    assert True\n"),
                commit("in role", ["tests/test_ok.py"]),
                self._write_summary(["tests/test_ok.py"]),
            ],
            phases=("test-writer",),
        )
        with register_runtime("fake", hostile):
            pipe.phase_test_writer()
        verify = (pipe.work / "git" / "verify-test-writer.md").read_text(encoding="utf-8")
        self.assertNotIn("(none — advisory)", verify)
        lowered = verify.lower()
        self.assertTrue(
            "head" in lowered or "commit" in lowered,
            "in-role commit must be recorded as a distinct non-green verify line, got:\n%s"
            % verify,
        )
        self.assertNotIn("no new paths (continuing)", "\n".join(pipe.log_lines))

    def test_unrelated_dirty_file_is_not_a_violation(self):
        (self.repo / "NOTES").write_text("scratch\n", encoding="utf-8")
        pipe = self._pipe()
        hostile = HostileRuntime(
            [
                write("src/greet.py", "def greet():\n    return 'hello'\n"),
                self._write_summary(["src/greet.py"]),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            pipe.phase_implementer()
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertNotIn("violations:", verify)
        self.assertNotIn("NOTES", verify)

    def test_phase_that_changes_nothing_is_ok(self):
        pipe = self._pipe()
        hostile = HostileRuntime(
            [self._write_summary([])],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            pipe.phase_implementer()

    def test_unset_code_root_refuses_write_code_before_invoke(self):
        pipe = self._pipe(code_root="", test_root="tests")
        pipe.cfg.code_root = ""
        hostile = HostileRuntime(
            [self._write_summary(["src/x.py"])],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        msg = str(ctx.exception)
        self.assertIn("code_root", msg)
        self.assertIn("explicit", msg.lower())
        write_calls = [c for c in hostile.calls if c["capability"] == "write-code"]
        self.assertEqual(write_calls, [])
        self.assertFalse((pipe.work / "git" / "verify-implementer.md").is_file())

    def test_whitespace_code_root_is_unset(self):
        pipe = self._pipe(code_root="   ", test_root="tests")
        pipe.cfg.code_root = "   "
        hostile = HostileRuntime([self._write_summary([])], phases=("implementer",))
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        self.assertIn("code_root", str(ctx.exception))
        self.assertEqual(
            [c for c in hostile.calls if c["capability"] == "write-code"],
            [],
        )

    def test_dot_code_root_is_explicit_whole_repo(self):
        pipe = self._pipe(code_root=".", test_root="tests")
        pipe.cfg.code_root = "."
        hostile = HostileRuntime(
            [
                write("top.py", "x\n"),
                self._write_summary(["top.py"]),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            try:
                pipe.phase_implementer()
            except PipelineError as exc:
                self.fail("'.' is an explicit whole-repo root, got %s" % exc)
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertIn("allowed_roots: .", verify)
        self.assertNotIn("(none — advisory)", verify)
        self.assertNotIn("violations:", verify)

    def test_apply_range_refuses_write_with_unset_roots(self):
        rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "review-since-tag"
        state = State.load(work)
        self.assertEqual(state.code_root, "")
        self.assertEqual(state.test_root, "")
        dump_json(
            work / "prompts" / "reviewer-fake.result.json",
            {
                "summary": "injected",
                "findings": [
                    {
                        "severity": "high",
                        "title": "readme typo as stand-in bug",
                        "evidence": "README",
                        "path": "README",
                        "kind": "implementation",
                    }
                ],
            },
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "--no-review",
                "review-since-tag",
            ]
        )
        self.assertNotEqual(rc, 0)
        state = State.load(work)
        self.assertNotEqual(state.stop_reason, "applied")


if __name__ == "__main__":
    unittest.main()
