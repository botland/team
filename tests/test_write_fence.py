"""Write fence: content-and-commit aware; empty roots fail closed.

Role roots fail closed. Already-dirty under the hop's root is allowed.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.cli import main
from team.config import load_config
from team.gitutil import (
    outside_blobs,
    outside_changed,
    outside_snapshot,
    revert_outside,
    snapshot,
    verify_delta,
)
from team.pipeline import PipelineError, load_pipeline, start_feature
from team.state import State
from team.util import denied_write_code_roots, dump_json, under_root
from tests.support.hostile import (
    HostileRuntime,
    commit,
    delete,
    emit,
    register_runtime,
    rename,
    write,
)
from tests.support.repo import git, head_sha, init_repo
from tests.support.verify_report import (
    ALREADY_DIRTY_HEADING,
    VIOLATIONS_HEADING,
    has_heading,
    heading_paths,
)

GATE_READY = {"ready": True, "consult": "none", "questions": []}
WRITE_SUMMARY = lambda paths: {"summary": "hostile", "paths_touched": list(paths)}
DIRTY_FENCE_MARKERS = ("mutated already-dirty paths", "dirty since run start")


def _fence_route(pipe, capability: str) -> str:
    """Which verifier _fence_after_invoke picks for a capability.

    Behavioural, not a regex over pipeline.py. The fence moved onto
    ``capability`` inside one dispatcher, so the old source census -- literal
    phase names at ``self._verify_write("name"`` call sites -- matched nothing
    and every ``assertIn`` in it failed while its ``assertNotIn`` passed. A
    census that can only under-draw is worse than none.
    """
    seen = []
    pipe._verify_write_tests = lambda phase, before: seen.append("write-tests")
    pipe._verify_write_code = lambda phase, before: seen.append("write-code")
    pipe._fence_readonly = lambda phase, before: seen.append("read-only")
    pipe._fence_after_invoke(capability, "probe", {"head": "", "paths": [], "entries": {}})
    return seen[0] if seen else ""


def _assert_in_role_already_dirty(test, verify: str, rel: str) -> None:
    test.assertTrue(
        has_heading(verify, ALREADY_DIRTY_HEADING),
        "missing exact heading %r in:\n%s" % (ALREADY_DIRTY_HEADING, verify),
    )
    test.assertIn(rel, heading_paths(verify, ALREADY_DIRTY_HEADING))
    test.assertNotIn(rel, heading_paths(verify, VIOLATIONS_HEADING))


def _assert_outside_root_error(test, exc: BaseException, rel: str) -> None:
    msg = str(exc)
    test.assertIn(rel, msg)
    test.assertIn("outside allowed roots", msg)
    for marker in DIRTY_FENCE_MARKERS:
        test.assertNotIn(marker, msg)


def _assert_forbidden_tree_restored(
    test,
    repo: Path,
    *,
    rel: str,
    head_before: str,
    pre_bytes: str | None = None,
    created: bool = False,
    dest_rel: str | None = None,
) -> None:
    """Out-of-role bytes/commits are gone. Exception text is not this property."""
    test.assertEqual(head_sha(repo), head_before)
    test.assertNotEqual(git(repo, "log", "-1", "--format=%s").strip(), "hostile write")
    path = repo / rel
    if created:
        test.assertFalse(path.exists(), "%s must be gone from the worktree" % rel)
        names = git(repo, "ls-tree", "-r", "--name-only", "HEAD")
        test.assertNotIn(rel, names.splitlines())
        test.assertEqual(git(repo, "status", "--porcelain", "--", rel).strip(), "")
    else:
        test.assertTrue(path.is_file(), "%s must exist with pre-hop bytes" % rel)
        if pre_bytes is not None:
            test.assertEqual(path.read_text(encoding="utf-8"), pre_bytes)
    if dest_rel:
        test.assertFalse(
            (repo / dest_rel).exists(),
            "git-mv dest %s must not remain" % dest_rel,
        )


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
        """Path geometry only: '.' contains every relative path. Not a write grant."""
        self.assertTrue(under_root("src/x.py", "."))
        self.assertTrue(under_root("foo.py", "."))
        self.assertTrue(under_root("tests/a.py", "./"))

    def test_dot_write_code_denies_test_root_and_foreign_submodules(self):
        """code_root='.' is the repo minus test_root and foreign submodules.

        under_root('.', 'tests/a.py') is True (geometry). The fence decision
        is verify_delta + denied_write_code_roots, not that helper alone.
        """
        denied = denied_write_code_roots(".", "tests", ["appliance-console"])
        self.assertIn("tests", denied)
        self.assertIn("appliance-console", denied)
        self.assertTrue(any(under_root("tests/a.py", root) for root in denied))
        ok, bad = verify_delta(
            ["top.py", "tests/a.py", "appliance-console/page.tsx", "schemas/g.json"],
            ["."],
            denied_roots=denied,
        )
        self.assertEqual(bad, ["tests/a.py", "appliance-console/page.tsx"])
        self.assertIn("top.py", ok)
        self.assertIn("schemas/g.json", ok)
        self.assertNotIn("tests/a.py", ok)


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
        self.seed_sha = head_sha(self.repo)

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

    def test_in_role_already_dirty_persists_and_is_not_a_violation(self):
        keep = self.repo / "src" / "keep.py"
        keep.write_text("user-wip\n", encoding="utf-8")
        pipe = self._pipe(slug="persist-wip")
        hostile = HostileRuntime(
            [
                write("src/keep.py", "user-wip\npatched\n"),
                self._write_summary(["src/keep.py"]),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            try:
                pipe.phase_implementer()
            except PipelineError as exc:
                self.fail("in-role already-dirty must persist, got %s" % exc)
        self.assertEqual(keep.read_text(encoding="utf-8"), "user-wip\npatched\n")
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        _assert_in_role_already_dirty(self, verify, "src/keep.py")

    def test_already_dirty_edit_under_root_is_ok(self):
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
            try:
                pipe.phase_implementer()
            except PipelineError as exc:
                self.fail("dirty file under code_root must stay writable, got %s" % exc)
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        _assert_in_role_already_dirty(self, verify, "src/x.py")

    def test_delete_under_other_root_clean_is_a_violation(self):
        pipe = self._pipe()
        head_before = head_sha(self.repo)
        target = self.repo / "tests" / "test_a.py"
        self.assertEqual(target.read_text(encoding="utf-8"), "ok\n")
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
        self.assertIn("tests/test_a.py", heading_paths(verify, VIOLATIONS_HEADING))
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="tests/test_a.py",
            head_before=head_before,
            pre_bytes="ok\n",
        )

    def test_delete_under_other_root_already_dirty_is_a_violation(self):
        (self.repo / "tests" / "test_a.py").write_text("dirty\n", encoding="utf-8")
        pipe = self._pipe()
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [delete("tests/test_a.py"), self._write_summary(["tests/test_a.py"])],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        _assert_outside_root_error(self, ctx.exception, "tests/test_a.py")
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertIn("tests/test_a.py", heading_paths(verify, VIOLATIONS_HEADING))
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="tests/test_a.py",
            head_before=head_before,
            pre_bytes="dirty\n",
        )

    def test_commit_of_out_of_role_writes_is_a_violation(self):
        pipe = self._pipe()
        head_before = head_sha(self.repo)
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
        self.assertIn("src/x.py", heading_paths(verify, VIOLATIONS_HEADING))
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="src/x.py",
            head_before=head_before,
            created=True,
        )

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

    def test_later_hop_may_edit_this_run_dirty_file(self):
        pipe = self._pipe()
        first = HostileRuntime(
            [
                write("src/x.py", "orig\n"),
                self._write_summary(["src/x.py"]),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", first):
            pipe.phase_implementer()
        second = HostileRuntime(
            [
                write("src/x.py", "orig\nmore\n"),
                self._write_summary(["src/x.py"]),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", second):
            try:
                pipe.phase_implementer()
            except PipelineError as exc:
                self.fail(
                    "this-run dirty file under code_root must stay writable, got %s" % exc
                )
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertNotIn("src/x.py", heading_paths(verify, VIOLATIONS_HEADING))
        self.assertNotIn("src/x.py", heading_paths(verify, ALREADY_DIRTY_HEADING))

    def test_repair_may_edit_this_run_tests(self):
        pipe = self._pipe()
        writer = HostileRuntime(
            [
                write("tests/test_new.py", "def test_a():\n    assert True\n"),
                self._write_summary(["tests/test_new.py"]),
            ],
            phases=("test-writer",),
        )
        with register_runtime("fake", writer):
            pipe.phase_test_writer()
        pipe.state.diagnosis_owner = "test-writer"
        repairer = HostileRuntime(
            [
                write(
                    "tests/test_new.py",
                    "def test_a():\n    assert True\n    assert 1\n",
                ),
                self._write_summary(["tests/test_new.py"]),
            ],
            phases=("repair-test-writer",),
        )
        with register_runtime("fake", repairer):
            try:
                pipe.phase_repair()
            except PipelineError as exc:
                self.fail("repair must be allowed to patch this-run tests, got %s" % exc)
        verify = (pipe.work / "git" / "verify-repair.md").read_text(encoding="utf-8")
        self.assertNotIn("violations:", verify)
        self.assertIn("tests/test_new.py", verify)

    def test_repair_may_edit_user_dirty_test(self):
        (self.repo / "tests" / "test_a.py").write_text("user\n", encoding="utf-8")
        pipe = self._pipe()
        pipe.state.diagnosis_owner = "test-writer"
        repairer = HostileRuntime(
            [
                write("tests/test_a.py", "user\npatched\n"),
                self._write_summary(["tests/test_a.py"]),
            ],
            phases=("repair-test-writer",),
        )
        with register_runtime("fake", repairer):
            try:
                pipe.phase_repair()
            except PipelineError as exc:
                self.fail("repair may edit a user-dirty test under test_root, got %s" % exc)
        verify = (pipe.work / "git" / "verify-repair.md").read_text(encoding="utf-8")
        _assert_in_role_already_dirty(self, verify, "tests/test_a.py")

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
        self.assertNotIn("NOTES", heading_paths(verify, VIOLATIONS_HEADING))
        self.assertNotIn("NOTES", heading_paths(verify, ALREADY_DIRTY_HEADING))
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
                write("schemas/guardian.json", "{}\n"),
                write("status/CURRENT.md", "ok\n"),
                self._write_summary(["top.py", "schemas/guardian.json", "status/CURRENT.md"]),
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
        self.assertIn("denied_roots: tests", verify)
        self.assertNotIn("(none — advisory)", verify)
        self.assertNotIn("violations:", verify)

    def test_dot_code_root_cannot_write_test_root(self):
        pipe = self._pipe(code_root=".", test_root="tests")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [
                write("tests/test_a.py", "pwned\n"),
                self._write_summary(["tests/test_a.py"]),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        self.assertIn("tests/test_a.py", str(ctx.exception))
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertIn("violations:", verify)
        self.assertIn("tests/test_a.py", verify)
        self.assertIn("denied_roots: tests", verify)
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="tests/test_a.py",
            head_before=head_before,
            pre_bytes="ok\n",
        )

    def test_dot_test_root_cannot_write_code_root(self):
        """Converse of code_root='.': test_root='.' still excludes production."""
        pipe = self._pipe(code_root="src", test_root=".", slug="dot-test-root")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            by_phase={
                "test-writer-gate": [emit(GATE_READY)],
                "test-writer": [
                    write("src/keep.py", "pwned\n"),
                    emit(WRITE_SUMMARY(["src/keep.py"])),
                ],
            }
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_test_writer()
        self.assertIn("src/keep.py", str(ctx.exception))
        verify = (pipe.work / "git" / "verify-test-writer.md").read_text(encoding="utf-8")
        self.assertIn("violations:", verify)
        self.assertIn("src/keep.py", heading_paths(verify, VIOLATIONS_HEADING))
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="src/keep.py",
            head_before=head_before,
            pre_bytes="keep\n",
        )

    def test_dot_code_root_cannot_write_foreign_submodule(self):
        from tests.support.repo import git as _git

        (self.repo / ".gitmodules").write_text(
            '[submodule "appliance-console"]\n'
            "\tpath = appliance-console\n"
            "\turl = git@example.com:console.git\n",
            encoding="utf-8",
        )
        (self.repo / "appliance-console").mkdir()
        (self.repo / "appliance-console" / "page.tsx").write_text("old\n", encoding="utf-8")
        _git(self.repo, "add", ".gitmodules", "appliance-console/page.tsx")
        _git(self.repo, "commit", "-m", "submodule tree")
        pipe = self._pipe(code_root=".", test_root="tests", slug="submod")
        hostile = HostileRuntime(
            [
                write("appliance-console/page.tsx", "pwned\n"),
                write("ARCHITECTURE_TOOLS.md", "doc\n"),
                self._write_summary(
                    ["appliance-console/page.tsx", "ARCHITECTURE_TOOLS.md"]
                ),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        msg = str(ctx.exception)
        self.assertIn("appliance-console/page.tsx", msg)
        self.assertNotIn("ARCHITECTURE_TOOLS.md", msg)
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertIn("denied_roots: tests, appliance-console", verify)
        self.assertIn("appliance-console/page.tsx", verify)
        self.assertIn("ARCHITECTURE_TOOLS.md", verify)
        viol = verify.split("violations:", 1)[1]
        self.assertIn("appliance-console/page.tsx", viol)
        self.assertNotIn("ARCHITECTURE_TOOLS.md", viol.split("already_dirty")[0])

    def test_inspect_phase_product_write_is_fence_error(self):
        pipe = self._pipe(slug="inspect-fence")
        product = "src/keep.py"
        review_out = {"summary": "ok", "findings": [], "review_markdown": "ok"}
        guardian_out = {
            "risks": [],
            "chain": {
                "r_to_a": {"ok": True, "note": "n"},
                "a_to_t": {"ok": True, "note": "n"},
                "t_to_i": {"ok": True, "note": "n"},
                "i_to_r": {"ok": True, "note": "n"},
            },
            "guardian_markdown": "ok",
        }
        critic_out = {
            "accepts": True,
            "issues": [],
            "attacks": [],
            "critic_markdown": "ok",
        }
        architect_out = {
            "design_markdown": "# Design\n",
            "code_root": "src",
            "test_root": "tests",
            "acceptance_criteria": [],
            "structural_touchpoints": [],
            "invariants": [],
        }
        tdd_out = {
            "ready": True,
            "questions": [],
            "test_contract_markdown": "# Contract\n",
            "criteria_map": [],
        }
        debugger_out = {
            "owner": "implementer",
            "root_cause": "x",
            "diagnosis_markdown": "x",
            "disposition": "retry",
        }

        def run(phase_names, fn, emit_body):
            from tests.support.repo import git as _git
            from tests.support.repo import head_sha

            keep = self.repo / product
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.write_text("keep\n", encoding="utf-8")
            _git(self.repo, "checkout", "HEAD", "--", product)
            keep.write_text("keep\n", encoding="utf-8")
            head_before = head_sha(self.repo)
            hostile = HostileRuntime(
                [write(product, "pwned-by-inspect\n"), emit(emit_body)],
                phases=phase_names,
                num_turns=2,
            )
            with register_runtime("fake", hostile):
                with self.assertRaises(PipelineError) as ctx:
                    fn()
            self.assertIn(product, str(ctx.exception))
            self.assertTrue(keep.is_file(), "restore must leave %s as a file" % product)
            self.assertEqual(
                keep.read_text(encoding="utf-8"),
                "keep\n",
                "restore must revert pre-hop bytes, not unlink",
            )
            self.assertEqual(head_sha(self.repo), head_before)

        run(("reviewer-fake",), pipe.phase_reviewer, review_out)
        run(("guardian",), pipe.phase_guardian, guardian_out)
        run(("critic",), pipe.phase_critic, critic_out)
        run(("architect",), pipe.phase_architect, architect_out)
        run(("tdd-design",), pipe.phase_tdd_design, tdd_out)
        pipe.state.final = {"status": "FAIL", "failing": []}
        run(("debugger",), pipe.phase_debugger, debugger_out)
        seq_dir = pipe.work / "seq" / "deadbeefcafe"
        (seq_dir / "prompts").mkdir(parents=True)
        run(
            ("seq-reviewer-fake",),
            lambda: pipe.phase_seq_review(seq_dir, [{"kind": "test", "title": "x"}]),
            review_out,
        )
        run(
            ("seq-guardian",),
            lambda: pipe._phase_seq_guardian(seq_dir, [{"kind": "test", "title": "x"}]),
            guardian_out,
        )

    def test_already_dirty_outside_root_is_still_a_violation(self):
        (self.repo / "tests" / "test_a.py").write_text("dirty\n", encoding="utf-8")
        pipe = self._pipe()
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [write("tests/test_a.py", "rewritten\n"), self._write_summary(["tests/test_a.py"])],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        _assert_outside_root_error(self, ctx.exception, "tests/test_a.py")
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertIn("tests/test_a.py", heading_paths(verify, VIOLATIONS_HEADING))
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="tests/test_a.py",
            head_before=head_before,
            pre_bytes="dirty\n",
        )

    def test_unset_write_root_fails_closed_before_complete(self):
        self.test_unset_or_empty_write_root_fails_closed()

    def test_unset_or_empty_write_root_fails_closed(self):
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
        self.assertEqual([c for c in hostile.calls if c["capability"] == "write-code"], [])
        self.assertFalse((pipe.work / "git" / "verify-implementer.md").is_file())

        tw = self._pipe(code_root="src", test_root="", slug="unset-test-root")
        tw.cfg.test_root = ""
        tw_rt = HostileRuntime(
            [self._write_summary(["tests/test_a.py"])],
            phases=("test-writer",),
        )
        with register_runtime("fake", tw_rt):
            with self.assertRaises(PipelineError) as ctx:
                tw.phase_test_writer()
        self.assertIn("test_root", str(ctx.exception))
        self.assertIn("explicit", str(ctx.exception).lower())
        self.assertEqual([c for c in tw_rt.calls if c["capability"] == "write-tests"], [])
        self.assertFalse((tw.work / "git" / "verify-test-writer.md").is_file())

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
                "review-since-tag",
            ]
        )
        self.assertNotEqual(rc, 0)
        state = State.load(work)
        self.assertNotEqual(state.stop_reason, "applied")

    def test_every_write_role_routes_to_a_write_verifier(self):
        """Membership derives from ROLES, which is orthogonal to the fence code.

        The converse is asked too: every non-write role must land on the
        read-only fence rather than on nothing.
        """
        from team.config import ROLES, may_write

        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        init_repo(repo)
        cfg = load_config(repo, fake=True, force=True, code_root=".", test_root="tests")
        pipe = start_feature(cfg, "brief", "fence-route")

        checked = 0
        for role, spec in ROLES.items():
            cap = spec["capability"]
            route = _fence_route(pipe, cap)
            if may_write(cap):
                self.assertIn(
                    route,
                    ("write-tests", "write-code"),
                    "%s (%s) is a write role with no write verifier" % (role, cap),
                )
            else:
                self.assertEqual(
                    route, "read-only", "%s (%s) took no fence" % (role, cap)
                )
            checked += 1
        self.assertTrue(checked, "ROLES census was empty; this asserts nothing")

    def test_already_dirty_write_census_does_not_raise(self):
        cases = [
            (
                "test-writer",
                "tests/test_a.py",
                "git/verify-test-writer.md",
                {
                    "test-writer-gate": [emit(GATE_READY)],
                    "test-writer": [
                        write("tests/test_a.py", "patched-tw\n"),
                        emit(WRITE_SUMMARY(["tests/test_a.py"])),
                    ],
                },
                lambda pipe: pipe.phase_test_writer(),
            ),
            (
                "implementer",
                "src/keep.py",
                "git/verify-implementer.md",
                {
                    "implementer-gate": [emit(GATE_READY)],
                    "implementer": [
                        write("src/keep.py", "patched-impl\n"),
                        emit(WRITE_SUMMARY(["src/keep.py"])),
                    ],
                },
                lambda pipe: pipe.phase_implementer(),
            ),
            (
                "repair-test",
                "tests/test_a.py",
                "git/verify-repair.md",
                {
                    "repair-test-writer": [
                        write("tests/test_a.py", "patched-repair-t\n"),
                        emit(WRITE_SUMMARY(["tests/test_a.py"])),
                    ],
                },
                lambda pipe: (
                    setattr(pipe.state, "diagnosis_owner", "test-writer"),
                    pipe.phase_repair(),
                )[-1],
            ),
            (
                "repair-code",
                "src/keep.py",
                "git/verify-repair.md",
                {
                    "repair-implementer": [
                        write("src/keep.py", "patched-repair-c\n"),
                        emit(WRITE_SUMMARY(["src/keep.py"])),
                    ],
                },
                lambda pipe: (
                    setattr(pipe.state, "diagnosis_owner", "implementer"),
                    pipe.phase_repair(),
                )[-1],
            ),
            (
                "adversarial",
                "tests/test_a.py",
                "git/verify-adversarial.md",
                {
                    "adversarial": [
                        write("tests/test_a.py", "patched-adv\n"),
                        emit(
                            {
                                "vectors": [],
                                "adversarial_markdown": "ok",
                                "paths_touched": ["tests/test_a.py"],
                            }
                        ),
                    ],
                },
                lambda pipe: pipe.phase_adversarial(),
            ),
            (
                "apply-test-writer",
                "tests/test_a.py",
                "git/verify-apply-test-writer.md",
                {
                    "test-writer-apply": [
                        write("tests/test_a.py", "patched-apply-t\n"),
                        emit(WRITE_SUMMARY(["tests/test_a.py"])),
                    ],
                },
                lambda pipe: pipe._apply_test_writer(
                    [
                        {
                            "kind": "test",
                            "title": "t",
                            "path": "tests/test_a.py",
                            "severity": "high",
                            "evidence": "e",
                        }
                    ],
                    thin=True,
                ),
            ),
            (
                "apply-implementer",
                "src/keep.py",
                "git/verify-apply-implementer.md",
                {
                    "implementer-apply": [
                        write("src/keep.py", "patched-apply-i\n"),
                        emit(WRITE_SUMMARY(["src/keep.py"])),
                    ],
                },
                lambda pipe: pipe._apply_implementer(
                    [
                        {
                            "kind": "implementation",
                            "title": "i",
                            "path": "src/keep.py",
                            "severity": "high",
                            "evidence": "e",
                        }
                    ],
                    thin=True,
                ),
            ),
        ]
        driven = {c[0] for c in cases}
        self.assertTrue(
            {"test-writer", "implementer", "adversarial", "apply-test-writer", "apply-implementer"}
            <= driven
        )
        self.assertIn("repair-test", driven)
        self.assertIn("repair-code", driven)

        for name, rel, verify_rel, by_phase, drive in cases:
            with self.subTest(hop=name):
                expected = {
                    "test-writer": "patched-tw\n",
                    "implementer": "patched-impl\n",
                    "repair-test": "patched-repair-t\n",
                    "repair-code": "patched-repair-c\n",
                    "adversarial": "patched-adv\n",
                    "apply-test-writer": "patched-apply-t\n",
                    "apply-implementer": "patched-apply-i\n",
                }[name]
                (self.repo / rel).write_text("user-wip-%s\n" % name, encoding="utf-8")
                pipe = self._pipe(slug="census-%s" % name.replace("/", "-"))
                hostile = HostileRuntime(by_phase=by_phase)
                with register_runtime("fake", hostile):
                    try:
                        drive(pipe)
                    except PipelineError as exc:
                        self.fail("%s in-role already-dirty must not raise: %s" % (name, exc))
                self.assertEqual(
                    (self.repo / rel).read_text(encoding="utf-8"),
                    expected,
                    "%s must persist the hop's in-role bytes" % name,
                )
                verify = (pipe.work / verify_rel).read_text(encoding="utf-8")
                _assert_in_role_already_dirty(self, verify, rel)
                log = "\n".join(pipe.log_lines)
                for marker in DIRTY_FENCE_MARKERS:
                    self.assertNotIn(marker, log)
                    self.assertNotIn(marker, pipe.state.stop_reason or "")

    def test_apply_seq_implementation_on_already_dirty_code_is_ok(self):
        (self.repo / "src" / "keep.py").write_text("user-wip\n", encoding="utf-8")
        pipe = self._pipe(slug="seq-dirty")
        pipe.cfg.test_command = "true"
        pipe.state.test_command = "true"
        dump_json(
            pipe.work / "prompts" / "reviewer-fake.result.json",
            {
                "summary": "injected",
                "findings": [
                    {
                        "severity": "high",
                        "title": "keep needs a guard",
                        "evidence": "no guard",
                        "path": "src/keep.py",
                        "kind": "implementation",
                    }
                ],
            },
        )
        pipe.write_artifact("review.md", "# Review\n")
        rec = dict(pipe.state.last_review or {})
        path = pipe.work / "prompts" / "reviewer-fake.result.json"
        rec["results"] = [
            {
                "name": path.name,
                "digest": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
            }
        ]
        rec["attempt"] = 1
        pipe.state.last_review = rec
        pipe.save()
        hostile = HostileRuntime(
            [
                write("src/keep.py", "user-wip\npatched\n"),
                emit(WRITE_SUMMARY(["src/keep.py"])),
            ],
            phases=("implementer-apply",),
        )
        with register_runtime("fake", hostile):
            pipe.apply_review(seq=True)
        self.assertNotEqual(pipe.state.stop_reason, "seq-failed")
        self.assertNotIn("failed", (pipe.state.stop_reason or ""))
        for marker in DIRTY_FENCE_MARKERS:
            self.assertNotIn(marker, pipe.state.stop_reason or "")
        verify = (pipe.work / "git" / "verify-apply-implementer.md").read_text(
            encoding="utf-8"
        )
        _assert_in_role_already_dirty(self, verify, "src/keep.py")

    def test_already_dirty_denied_test_root_or_submodule_is_still_a_violation(self):
        (self.repo / "tests" / "test_a.py").write_text("dirty\n", encoding="utf-8")
        pipe = self._pipe(code_root=".", test_root="tests", slug="denied-dirty-test")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [
                write("tests/test_a.py", "pwned\n"),
                emit(WRITE_SUMMARY(["tests/test_a.py"])),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        _assert_outside_root_error(self, ctx.exception, "tests/test_a.py")
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertIn("tests/test_a.py", heading_paths(verify, VIOLATIONS_HEADING))
        self.assertIn("denied", str(ctx.exception).lower())
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="tests/test_a.py",
            head_before=head_before,
            pre_bytes="dirty\n",
        )

        (self.repo / ".gitmodules").write_text(
            '[submodule "appliance-console"]\n'
            "\tpath = appliance-console\n"
            "\turl = git@example.com:console.git\n",
            encoding="utf-8",
        )
        (self.repo / "appliance-console").mkdir()
        (self.repo / "appliance-console" / "page.tsx").write_text("old\n", encoding="utf-8")
        git(self.repo, "add", ".gitmodules", "appliance-console/page.tsx")
        git(self.repo, "commit", "-m", "submodule tree")
        (self.repo / "appliance-console" / "page.tsx").write_text("dirty\n", encoding="utf-8")
        pipe = self._pipe(code_root=".", test_root="tests", slug="denied-dirty-sub")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [
                write("appliance-console/page.tsx", "pwned\n"),
                emit(WRITE_SUMMARY(["appliance-console/page.tsx"])),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        _assert_outside_root_error(self, ctx.exception, "appliance-console/page.tsx")
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertIn(
            "appliance-console/page.tsx", heading_paths(verify, VIOLATIONS_HEADING)
        )
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="appliance-console/page.tsx",
            head_before=head_before,
            pre_bytes="dirty\n",
        )

    def test_already_dirty_is_not_a_write_grant_outside_role(self):
        self.test_already_dirty_census_converse_is_still_outside_root()

    def test_already_dirty_census_converse_is_still_outside_root(self):
        (self.repo / "src" / "keep.py").write_text("dirty-code\n", encoding="utf-8")
        pipe = self._pipe(slug="tw-dirty-code")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            by_phase={
                "test-writer-gate": [emit(GATE_READY)],
                "test-writer": [
                    write("src/keep.py", "stolen\n"),
                    emit(WRITE_SUMMARY(["src/keep.py"])),
                ],
            }
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_test_writer()
        _assert_outside_root_error(self, ctx.exception, "src/keep.py")
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="src/keep.py",
            head_before=head_before,
            pre_bytes="dirty-code\n",
        )

        (self.repo / "tests" / "test_a.py").write_text("dirty-test\n", encoding="utf-8")
        pipe = self._pipe(slug="impl-dirty-test")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            by_phase={
                "implementer-gate": [emit(GATE_READY)],
                "implementer": [
                    write("tests/test_a.py", "stolen\n"),
                    emit(WRITE_SUMMARY(["tests/test_a.py"])),
                ],
            }
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        _assert_outside_root_error(self, ctx.exception, "tests/test_a.py")
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="tests/test_a.py",
            head_before=head_before,
            pre_bytes="dirty-test\n",
        )

    def test_verify_already_dirty_heading_is_not_under_violations(self):
        target = self.repo / "src" / "x.py"
        target.write_text("orig\n", encoding="utf-8")
        pipe = self._pipe(slug="heading-sib")
        hostile = HostileRuntime(
            [
                write("src/x.py", "more\n", append=True),
                self._write_summary(["src/x.py"]),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            pipe.phase_implementer()
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertTrue(has_heading(verify, ALREADY_DIRTY_HEADING))
        self.assertIn("src/x.py", heading_paths(verify, ALREADY_DIRTY_HEADING))
        self.assertNotIn("src/x.py", heading_paths(verify, VIOLATIONS_HEADING))
        viol_idx = verify.find(VIOLATIONS_HEADING)
        dirty_idx = verify.find(ALREADY_DIRTY_HEADING)
        if viol_idx >= 0 and dirty_idx >= 0:
            self.assertGreater(dirty_idx, viol_idx)

    def test_pipeline_error_and_apply_never_name_already_dirty_as_failure(self):
        (self.repo / "src" / "keep.py").write_text("user-wip\n", encoding="utf-8")
        pipe = self._pipe(slug="no-dirty-fail")
        dump_json(
            pipe.work / "prompts" / "reviewer-fake.result.json",
            {
                "summary": "injected",
                "findings": [
                    {
                        "severity": "high",
                        "title": "keep needs a guard",
                        "evidence": "e",
                        "path": "src/keep.py",
                        "kind": "implementation",
                    }
                ],
            },
        )
        pipe.write_artifact("review.md", "# Review\n")
        path = pipe.work / "prompts" / "reviewer-fake.result.json"
        pipe.state.last_review = {
            "attempt": 1,
            "results": [
                {
                    "name": path.name,
                    "digest": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
                }
            ],
        }
        pipe.save()
        hostile = HostileRuntime(
            [
                write("src/keep.py", "user-wip\npatched\n"),
                emit(WRITE_SUMMARY(["src/keep.py"])),
            ],
            phases=("implementer-apply",),
        )
        with register_runtime("fake", hostile):
            pipe.apply_review()
        self.assertEqual(pipe.state.stop_reason, "applied")
        blobs = []
        for name in ("apply-summary.md", "apply-impl-summary.md", "apply-seq.md"):
            p = pipe.work / name
            if p.is_file():
                blobs.append(p.read_text(encoding="utf-8"))
        text = "\n".join(blobs)
        for marker in DIRTY_FENCE_MARKERS:
            self.assertNotIn(marker, text)

        (self.repo / "tests" / "test_a.py").write_text("dirty\n", encoding="utf-8")
        pipe = self._pipe(slug="out-role-dirty-err")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [write("tests/test_a.py", "x\n"), emit(WRITE_SUMMARY(["tests/test_a.py"]))],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        _assert_outside_root_error(self, ctx.exception, "tests/test_a.py")
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="tests/test_a.py",
            head_before=head_before,
            pre_bytes="dirty\n",
        )

    def test_already_dirty_in_role_delete_is_ok(self):
        (self.repo / "src" / "x.py").write_text("dirty\n", encoding="utf-8")
        pipe = self._pipe(slug="dirty-del")
        hostile = HostileRuntime(
            [delete("src/x.py"), emit(WRITE_SUMMARY(["src/x.py"]))],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            try:
                pipe.phase_implementer()
            except PipelineError as exc:
                self.fail("in-role delete of run-start-dirty must succeed, got %s" % exc)
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertNotIn("src/x.py", heading_paths(verify, VIOLATIONS_HEADING))

    def test_already_dirty_in_role_revert_to_head_is_ok(self):
        keep = self.repo / "src" / "keep.py"
        keep.write_text("dirty\n", encoding="utf-8")
        pipe = self._pipe(slug="dirty-revert")

        def _checkout(repo):
            git(repo, "checkout", "HEAD", "--", "src/keep.py")

        hostile = HostileRuntime(
            [emit(WRITE_SUMMARY(["src/keep.py"]))],
            phases=("implementer",),
        )
        orig = hostile._run_actions

        def with_checkout(actions, *, repo, session_id, phase=""):
            _checkout(repo)
            return orig(actions, repo=repo, session_id=session_id, phase=phase)

        hostile._run_actions = with_checkout
        with register_runtime("fake", hostile):
            try:
                pipe.phase_implementer()
            except PipelineError as exc:
                self.fail("in-role revert-to-HEAD of run-start-dirty must succeed, got %s" % exc)
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertNotIn("src/keep.py", heading_paths(verify, VIOLATIONS_HEADING))

    def test_already_dirty_in_role_commit_is_ok(self):
        (self.repo / "src" / "x.py").write_text("dirty\n", encoding="utf-8")
        pipe = self._pipe(slug="dirty-commit")
        hostile = HostileRuntime(
            [
                commit("user dirty", ["src/x.py"]),
                emit(WRITE_SUMMARY(["src/x.py"])),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            try:
                pipe.phase_implementer()
            except PipelineError as exc:
                self.fail("in-role commit of run-start-dirty must succeed, got %s" % exc)
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertNotIn("src/x.py", heading_paths(verify, VIOLATIONS_HEADING))
        lowered = verify.lower()
        self.assertTrue("head" in lowered or "commit" in lowered, verify)

    def test_protocol_paths_never_appear_as_already_dirty_on_verify(self):
        proto = self.repo / ".team" / "work" / "other" / "scratch.md"
        proto.parent.mkdir(parents=True, exist_ok=True)
        proto.write_text("protocol dirty\n", encoding="utf-8")
        pipe = self._pipe(slug="proto-dirty")
        hostile = HostileRuntime(
            [
                write("src/keep.py", "more\n", append=True),
                emit(WRITE_SUMMARY(["src/keep.py"])),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            pipe.phase_implementer()
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        for rel in heading_paths(verify, ALREADY_DIRTY_HEADING):
            self.assertFalse(
                rel.startswith(".team/work"),
                "protocol path listed as already_dirty: %s" % rel,
            )

    def test_already_dirty_does_not_relax_unset_roots(self):
        (self.repo / "src" / "keep.py").write_text("dirty\n", encoding="utf-8")
        pipe = self._pipe(code_root="", test_root="tests", slug="dirty-unset")
        pipe.cfg.code_root = ""
        hostile = HostileRuntime(
            [write("src/keep.py", "x\n"), emit(WRITE_SUMMARY(["src/keep.py"]))],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        msg = str(ctx.exception)
        self.assertIn("code_root", msg)
        self.assertIn("explicit", msg.lower())
        self.assertEqual([c for c in hostile.calls if c["capability"] == "write-code"], [])
        self.assertFalse((pipe.work / "git" / "verify-implementer.md").is_file())

    def test_withdrawn_already_dirty_as_violation_names_are_gone(self):
        loader = unittest.defaultTestLoader
        suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
        names = set()

        def walk(node):
            if hasattr(node, "_tests"):
                for child in node:
                    walk(child)
                return
            ident = getattr(node, "id", lambda: "")()
            names.add(ident.rsplit(".", 1)[-1])

        walk(suite)
        self.assertNotIn("test_already_dirty_edit_is_a_violation", names)
        self.assertNotIn("test_repair_cannot_edit_user_dirty_test", names)
        require_err = re.compile(
            r"assert(?:In|Regex)\(\s*[\"'][^\"']*(?:mutated already-dirty paths|already-dirty paths)"
        )
        for path in (ROOT / "tests").rglob("test_*.py"):
            text = path.read_text(encoding="utf-8")
            hit = require_err.search(text)
            self.assertIsNone(
                hit,
                "%s still requires withdrawn dirty-fence error: %s" % (path, hit.group(0) if hit else ""),
            )

    def test_missing_git_start_does_not_fail_closed(self):
        (self.repo / "src" / "x.py").write_text("dirty\n", encoding="utf-8")
        pipe = self._pipe(slug="no-start")
        git_state = dict(pipe.state.git or {})
        git_state.pop("start", None)
        pipe.state.git = git_state
        pipe.save()
        hostile = HostileRuntime(
            [write("src/x.py", "more\n", append=True), emit(WRITE_SUMMARY(["src/x.py"]))],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            try:
                pipe.phase_implementer()
            except PipelineError as exc:
                self.fail("missing state.git.start must not fail closed, got %s" % exc)

    def test_implementer_git_mv_from_test_root_is_a_violation(self):
        pipe = self._pipe(slug="mv-test-src")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [
                rename("tests/test_a.py", "src/stolen.py"),
                emit(WRITE_SUMMARY(["src/stolen.py"])),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        self.assertIn("tests/test_a.py", str(ctx.exception))
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertIn("tests/test_a.py", heading_paths(verify, VIOLATIONS_HEADING))
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="tests/test_a.py",
            head_before=head_before,
            pre_bytes="ok\n",
            dest_rel="src/stolen.py",
        )

    def test_test_writer_git_mv_from_code_root_is_a_violation(self):
        pipe = self._pipe(slug="mv-src-test")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            by_phase={
                "test-writer-gate": [emit(GATE_READY)],
                "test-writer": [
                    rename("src/keep.py", "tests/stolen.py"),
                    emit(WRITE_SUMMARY(["tests/stolen.py"])),
                ],
            }
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_test_writer()
        self.assertIn("src/keep.py", str(ctx.exception))
        verify = (pipe.work / "git" / "verify-test-writer.md").read_text(encoding="utf-8")
        self.assertIn("src/keep.py", heading_paths(verify, VIOLATIONS_HEADING))
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="src/keep.py",
            head_before=head_before,
            pre_bytes="keep\n",
            dest_rel="tests/stolen.py",
        )

    def test_in_role_git_mv_lists_both_paths_and_is_ok(self):
        pipe = self._pipe(slug="mv-in-role")
        hostile = HostileRuntime(
            by_phase={
                "test-writer-gate": [emit(GATE_READY)],
                "test-writer": [
                    rename("tests/test_a.py", "tests/test_b.py"),
                    emit(WRITE_SUMMARY(["tests/test_b.py"])),
                ],
            }
        )
        with register_runtime("fake", hostile):
            try:
                pipe.phase_test_writer()
            except PipelineError as exc:
                self.fail("in-role git mv must succeed, got %s" % exc)
        verify = (pipe.work / "git" / "verify-test-writer.md").read_text(encoding="utf-8")
        delta = heading_paths(verify, [ln for ln in verify.splitlines() if ln.startswith("delta (")][0])
        self.assertIn("tests/test_a.py", delta)
        self.assertIn("tests/test_b.py", delta)
        self.assertNotIn("tests/test_a.py", heading_paths(verify, VIOLATIONS_HEADING))
        self.assertNotIn("tests/test_b.py", heading_paths(verify, VIOLATIONS_HEADING))

    def _reset_seeded_tree(self) -> None:
        git(self.repo, "reset", "--hard", self.seed_sha)
        git(self.repo, "clean", "-fd")

    def _write_hop_specs(self):
        """Every write-capability hop × out-of-role path. Membership from pipeline."""
        return [
            {
                "name": "test-writer",
                "verify": "git/verify-test-writer.md",
                "out_mod": "src/keep.py",
                "out_create": "src/x.py",
                "pre_mod": "keep\n",
                "code_root": "src",
                "test_root": "tests",
                "emit": lambda paths: emit(WRITE_SUMMARY(paths)),
                "wrap": lambda acts: {
                    "test-writer-gate": [emit(GATE_READY)],
                    "test-writer": acts,
                },
                "drive": lambda pipe: pipe.phase_test_writer(),
            },
            {
                "name": "implementer",
                "verify": "git/verify-implementer.md",
                "out_mod": "tests/test_a.py",
                "out_create": "tests/pwned.py",
                "pre_mod": "ok\n",
                "code_root": "src",
                "test_root": "tests",
                "emit": lambda paths: emit(WRITE_SUMMARY(paths)),
                "wrap": lambda acts: {
                    "implementer-gate": [emit(GATE_READY)],
                    "implementer": acts,
                },
                "drive": lambda pipe: pipe.phase_implementer(),
            },
            {
                "name": "repair-test",
                "verify": "git/verify-repair.md",
                "out_mod": "src/keep.py",
                "out_create": "src/x.py",
                "pre_mod": "keep\n",
                "code_root": "src",
                "test_root": "tests",
                "emit": lambda paths: emit(WRITE_SUMMARY(paths)),
                "wrap": lambda acts: {"repair-test-writer": acts},
                "drive": lambda pipe: (
                    setattr(pipe.state, "diagnosis_owner", "test-writer"),
                    pipe.phase_repair(),
                )[-1],
            },
            {
                "name": "repair-code",
                "verify": "git/verify-repair.md",
                "out_mod": "tests/test_a.py",
                "out_create": "tests/pwned.py",
                "pre_mod": "ok\n",
                "code_root": "src",
                "test_root": "tests",
                "emit": lambda paths: emit(WRITE_SUMMARY(paths)),
                "wrap": lambda acts: {"repair-implementer": acts},
                "drive": lambda pipe: (
                    setattr(pipe.state, "diagnosis_owner", "implementer"),
                    pipe.phase_repair(),
                )[-1],
            },
            {
                "name": "adversarial",
                "verify": "git/verify-adversarial.md",
                "out_mod": "src/keep.py",
                "out_create": "src/x.py",
                "pre_mod": "keep\n",
                "code_root": "src",
                "test_root": "tests",
                "emit": lambda paths: emit(
                    {
                        "vectors": [],
                        "adversarial_markdown": "ok",
                        "paths_touched": list(paths),
                    }
                ),
                "wrap": lambda acts: {"adversarial": acts},
                "drive": lambda pipe: pipe.phase_adversarial(),
            },
            {
                "name": "apply-test-writer",
                "verify": "git/verify-apply-test-writer.md",
                "out_mod": "src/keep.py",
                "out_create": "src/x.py",
                "pre_mod": "keep\n",
                "code_root": "src",
                "test_root": "tests",
                "emit": lambda paths: emit(WRITE_SUMMARY(paths)),
                "wrap": lambda acts: {"test-writer-apply": acts},
                "drive": lambda pipe: pipe._apply_test_writer(
                    [
                        {
                            "kind": "test",
                            "title": "t",
                            "path": "tests/test_a.py",
                            "severity": "high",
                            "evidence": "e",
                        }
                    ],
                    thin=True,
                ),
            },
            {
                "name": "apply-implementer",
                "verify": "git/verify-apply-implementer.md",
                "out_mod": "tests/test_a.py",
                "out_create": "tests/pwned.py",
                "pre_mod": "ok\n",
                "code_root": "src",
                "test_root": "tests",
                "emit": lambda paths: emit(WRITE_SUMMARY(paths)),
                "wrap": lambda acts: {"implementer-apply": acts},
                "drive": lambda pipe: pipe._apply_implementer(
                    [
                        {
                            "kind": "implementation",
                            "title": "i",
                            "path": "src/keep.py",
                            "severity": "high",
                            "evidence": "e",
                        }
                    ],
                    thin=True,
                ),
            },
        ]

    def test_write_capability_out_of_role_shapes_restore_tree_and_head(self):
        from team.config import ROLES, may_write

        specs = self._write_hop_specs()
        driven = {s["name"] for s in specs}
        self.assertTrue(
            {"test-writer", "implementer", "adversarial", "apply-test-writer", "apply-implementer"}
            <= driven
        )
        self.assertIn("repair-test", driven)
        self.assertIn("repair-code", driven)
        # Membership derives from ROLES (orthogonal to this file), not from a
        # regex over pipeline.py that silently under-draws after a refactor.
        write_roles = {r for r, s in ROLES.items() if may_write(s["capability"])}
        self.assertTrue(write_roles, "ROLES census was empty; this asserts nothing")
        for role in sorted(write_roles):
            self.assertTrue(
                any(role in name for name in driven),
                "write role %s has no out-of-role mutation-check" % role,
            )
        self.assertNotIn("tester", driven)

        shapes = ("modify", "delete", "create", "commit", "dirty-further")

        def run_hop(spec, actions, slug):
            pipe = self._pipe(
                code_root=spec["code_root"],
                test_root=spec["test_root"],
                slug=slug,
            )
            head_before = head_sha(self.repo)
            hostile = HostileRuntime(by_phase=spec["wrap"](actions))
            with register_runtime("fake", hostile):
                with self.assertRaises(PipelineError) as ctx:
                    spec["drive"](pipe)
            verify = (pipe.work / spec["verify"]).read_text(encoding="utf-8")
            return pipe, ctx.exception, verify, head_before

        for spec in specs:
            for shape in shapes:
                with self.subTest(hop=spec["name"], shape=shape):
                    self._reset_seeded_tree()
                    if shape == "modify":
                        rel = spec["out_mod"]
                        actions = [
                            write(rel, "pwned-out\n"),
                            spec["emit"]([rel]),
                        ]
                        _, exc, verify, head_before = run_hop(
                            spec, actions, "oor-%s-mod" % spec["name"]
                        )
                        self.assertIn(rel, str(exc))
                        self.assertIn(rel, heading_paths(verify, VIOLATIONS_HEADING))
                        _assert_forbidden_tree_restored(
                            self,
                            self.repo,
                            rel=rel,
                            head_before=head_before,
                            pre_bytes=spec["pre_mod"],
                        )
                    elif shape == "delete":
                        rel = spec["out_mod"]
                        actions = [delete(rel), spec["emit"]([rel])]
                        _, exc, verify, head_before = run_hop(
                            spec, actions, "oor-%s-del" % spec["name"]
                        )
                        self.assertIn(rel, str(exc))
                        self.assertIn(rel, heading_paths(verify, VIOLATIONS_HEADING))
                        _assert_forbidden_tree_restored(
                            self,
                            self.repo,
                            rel=rel,
                            head_before=head_before,
                            pre_bytes=spec["pre_mod"],
                        )
                    elif shape == "create":
                        rel = spec["out_create"]
                        actions = [write(rel, "pwned-new\n"), spec["emit"]([rel])]
                        _, exc, verify, head_before = run_hop(
                            spec, actions, "oor-%s-new" % spec["name"]
                        )
                        self.assertIn(rel, str(exc))
                        self.assertIn(rel, heading_paths(verify, VIOLATIONS_HEADING))
                        _assert_forbidden_tree_restored(
                            self,
                            self.repo,
                            rel=rel,
                            head_before=head_before,
                            created=True,
                        )
                    elif shape == "commit":
                        rel = spec["out_create"]
                        actions = [
                            write(rel, "secret\n"),
                            commit("hostile write", [rel]),
                            spec["emit"]([rel]),
                        ]
                        _, exc, verify, head_before = run_hop(
                            spec, actions, "oor-%s-ci" % spec["name"]
                        )
                        self.assertIn(rel, str(exc))
                        self.assertIn(rel, heading_paths(verify, VIOLATIONS_HEADING))
                        _assert_forbidden_tree_restored(
                            self,
                            self.repo,
                            rel=rel,
                            head_before=head_before,
                            created=True,
                        )
                    else:
                        rel = spec["out_mod"]
                        (self.repo / rel).write_text("user-wip\n", encoding="utf-8")
                        actions = [write(rel, "pwned-further\n"), spec["emit"]([rel])]
                        _, exc, verify, head_before = run_hop(
                            spec, actions, "oor-%s-wip" % spec["name"]
                        )
                        self.assertIn(rel, str(exc))
                        self.assertIn(rel, heading_paths(verify, VIOLATIONS_HEADING))
                        _assert_forbidden_tree_restored(
                            self,
                            self.repo,
                            rel=rel,
                            head_before=head_before,
                            pre_bytes="user-wip\n",
                        )

        with self.subTest(hop="implementer", shape="git-mv"):
            self._reset_seeded_tree()
            spec = next(s for s in specs if s["name"] == "implementer")
            actions = [
                rename("tests/test_a.py", "src/stolen.py"),
                spec["emit"](["src/stolen.py"]),
            ]
            _, exc, verify, head_before = run_hop(spec, actions, "oor-impl-mv")
            self.assertIn("tests/test_a.py", str(exc))
            self.assertIn("tests/test_a.py", heading_paths(verify, VIOLATIONS_HEADING))
            _assert_forbidden_tree_restored(
                self,
                self.repo,
                rel="tests/test_a.py",
                head_before=head_before,
                pre_bytes="ok\n",
                dest_rel="src/stolen.py",
            )

        with self.subTest(hop="test-writer", shape="git-mv"):
            self._reset_seeded_tree()
            spec = next(s for s in specs if s["name"] == "test-writer")
            actions = [
                rename("src/keep.py", "tests/stolen.py"),
                spec["emit"](["tests/stolen.py"]),
            ]
            _, exc, verify, head_before = run_hop(spec, actions, "oor-tw-mv")
            self.assertIn("src/keep.py", str(exc))
            self.assertIn("src/keep.py", heading_paths(verify, VIOLATIONS_HEADING))
            _assert_forbidden_tree_restored(
                self,
                self.repo,
                rel="src/keep.py",
                head_before=head_before,
                pre_bytes="keep\n",
                dest_rel="tests/stolen.py",
            )

        impl = next(s for s in specs if s["name"] == "implementer")
        impl_dot = dict(impl, code_root=".", test_root="tests")
        with self.subTest(hop="implementer", shape="dot-deny-tests"):
            self._reset_seeded_tree()
            rel = "tests/test_a.py"
            actions = [write(rel, "pwned-dot\n"), impl_dot["emit"]([rel])]
            _, exc, verify, head_before = run_hop(impl_dot, actions, "oor-dot-tests")
            self.assertIn(rel, str(exc))
            self.assertIn(rel, heading_paths(verify, VIOLATIONS_HEADING))
            _assert_forbidden_tree_restored(
                self,
                self.repo,
                rel=rel,
                head_before=head_before,
                pre_bytes="ok\n",
            )

        with self.subTest(hop="implementer", shape="dot-deny-submodule"):
            self._reset_seeded_tree()
            (self.repo / ".gitmodules").write_text(
                '[submodule "appliance-console"]\n'
                "\tpath = appliance-console\n"
                "\turl = git@example.com:console.git\n",
                encoding="utf-8",
            )
            (self.repo / "appliance-console").mkdir()
            (self.repo / "appliance-console" / "page.tsx").write_text(
                "old\n", encoding="utf-8"
            )
            git(self.repo, "add", ".gitmodules", "appliance-console/page.tsx")
            git(self.repo, "commit", "-m", "submodule tree")
            rel = "appliance-console/page.tsx"
            actions = [write(rel, "pwned-sub\n"), impl_dot["emit"]([rel])]
            _, exc, verify, head_before = run_hop(impl_dot, actions, "oor-dot-sub")
            self.assertIn(rel, str(exc))
            self.assertIn(rel, heading_paths(verify, VIOLATIONS_HEADING))
            _assert_forbidden_tree_restored(
                self,
                self.repo,
                rel=rel,
                head_before=head_before,
                pre_bytes="old\n",
            )

    def test_write_capability_in_role_writes_still_persist(self):
        pipe = self._pipe(slug="in-role-impl")
        hostile = HostileRuntime(
            [
                write("src/greet.py", "def greet():\n    return 'hello'\n"),
                emit(WRITE_SUMMARY(["src/greet.py"])),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            pipe.phase_implementer()
        greet = self.repo / "src" / "greet.py"
        self.assertTrue(greet.is_file())
        self.assertIn("hello", greet.read_text(encoding="utf-8"))
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertNotIn("src/greet.py", heading_paths(verify, VIOLATIONS_HEADING))

        tw = self._pipe(slug="in-role-tw")
        writer = HostileRuntime(
            by_phase={
                "test-writer-gate": [emit(GATE_READY)],
                "test-writer": [
                    write("tests/test_ok.py", "def test_ok():\n    assert True\n"),
                    emit(WRITE_SUMMARY(["tests/test_ok.py"])),
                ],
            }
        )
        with register_runtime("fake", writer):
            tw.phase_test_writer()
        new_test = self.repo / "tests" / "test_ok.py"
        self.assertTrue(new_test.is_file())
        self.assertIn("assert True", new_test.read_text(encoding="utf-8"))

        self._reset_seeded_tree()
        (self.repo / "src" / "keep.py").write_text("user-wip\n", encoding="utf-8")
        dirty = self._pipe(slug="in-role-dirty")
        dirty_rt = HostileRuntime(
            by_phase={
                "implementer-gate": [emit(GATE_READY)],
                "implementer": [
                    write("src/keep.py", "user-wip\npatched\n"),
                    emit(WRITE_SUMMARY(["src/keep.py"])),
                ],
            }
        )
        with register_runtime("fake", dirty_rt):
            try:
                dirty.phase_implementer()
            except PipelineError as exc:
                self.fail("in-role already-dirty must persist, not restore-all: %s" % exc)
        self.assertEqual(
            (self.repo / "src" / "keep.py").read_text(encoding="utf-8"),
            "user-wip\npatched\n",
        )
        verify = (dirty.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        _assert_in_role_already_dirty(self, verify, "src/keep.py")

    def test_nested_test_root_under_code_root_is_the_same_hop(self):
        """Nested pair of roots is not a second disjoint src/ vs tests/ pair."""
        pkg_tests = self.repo / "pkg" / "tests"
        pkg_tests.mkdir(parents=True)
        (self.repo / "pkg" / "keep.py").write_text("keep\n", encoding="utf-8")
        (pkg_tests / "test_a.py").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "--", "pkg/keep.py", "pkg/tests/test_a.py")
        git(self.repo, "commit", "-m", "nested roots")
        pipe = self._pipe(code_root="pkg", test_root="pkg/tests", slug="nested-roots")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [
                write("pkg/tests/test_a.py", "pwned\n"),
                emit(WRITE_SUMMARY(["pkg/tests/test_a.py"])),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        self.assertIn("pkg/tests/test_a.py", str(ctx.exception))
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertIn("pkg/tests/test_a.py", heading_paths(verify, VIOLATIONS_HEADING))
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="pkg/tests/test_a.py",
            head_before=head_before,
            pre_bytes="ok\n",
        )

        head_before = head_sha(self.repo)
        mover = HostileRuntime(
            [
                rename("pkg/tests/test_a.py", "pkg/stolen.py"),
                emit(WRITE_SUMMARY(["pkg/stolen.py"])),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", mover):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        self.assertIn("pkg/tests/test_a.py", str(ctx.exception))
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertIn("pkg/tests/test_a.py", heading_paths(verify, VIOLATIONS_HEADING))
        _assert_forbidden_tree_restored(
            self,
            self.repo,
            rel="pkg/tests/test_a.py",
            head_before=head_before,
            pre_bytes="ok\n",
            dest_rel="pkg/stolen.py",
        )

        in_role = HostileRuntime(
            [
                write("pkg/ok.py", "def ok():\n    return 1\n"),
                emit(WRITE_SUMMARY(["pkg/ok.py"])),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", in_role):
            try:
                pipe.phase_implementer()
            except PipelineError as exc:
                self.fail("in-role write under nested code_root must persist, got %s" % exc)
        self.assertEqual(
            (self.repo / "pkg" / "ok.py").read_text(encoding="utf-8"),
            "def ok():\n    return 1\n",
        )


def _snap_keys_containing(snap, needle):
    want = needle.replace("\\", "/")
    return [key for key in snap if want in key.replace("\\", "/")]


class ExtraWorktreeFenceTests(unittest.TestCase):
    """Fence observer is not git porcelain of --repo only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "app"
        self.home = self.root / "home"
        self.elsewhere = self.root / "not-home"
        self.repo.mkdir()
        self.home.mkdir()
        self.elsewhere.mkdir()
        # Tests here point TEAM_HOME at `elsewhere` to prove it is not the
        # observer base for outside_snapshot -- but they then run a real hop,
        # which resolves schemas and personas from engine_root(). An empty
        # TEAM_HOME makes those tests die on a missing schema instead of
        # asserting what they are about, so `elsewhere` is a working engine
        # that simply is not $HOME.
        for name in ("schemas", "personas", "docs"):
            src = ROOT / name
            if src.is_dir():
                (self.elsewhere / name).symlink_to(src, target_is_directory=True)
        self._old_home = os.environ.get("HOME")
        self._old_team_home = os.environ.get("TEAM_HOME")
        os.environ["HOME"] = str(self.home)
        os.environ["TEAM_HOME"] = str(ROOT)
        init_repo(self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "keep.py").write_text("keep\n", encoding="utf-8")
        (self.repo / "tests" / "test_a.py").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "--", "src/keep.py", "tests/test_a.py")
        git(self.repo, "commit", "-m", "seed roots")

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        if self._old_team_home is None:
            os.environ.pop("TEAM_HOME", None)
        else:
            os.environ["TEAM_HOME"] = self._old_team_home
        self.tmp.cleanup()

    def _pipe(self, slug="extra-repo", code_root="src"):
        return start_feature(
            load_config(
                self.repo,
                fake=True,
                force=True,
                code_root=code_root,
                test_root="tests",
            ),
            "fence brief",
            slug,
        )

    def _outside_cases(self):
        return (
            ("absolute", str(self.root / "vibe.rc"), self.root / "vibe.rc"),
            ("parent-relative", "../vibe.rc", self.root / "vibe.rc"),
        )

    def _parent_team(self, *parts):
        return self.root.joinpath(".team", *parts)

    def _assert_home_isolated(self):
        isolated = Path.home().resolve()
        self.assertEqual(
            isolated,
            self.home.resolve(),
            "HOME isolation failed; refusing to write the real home (%s)" % isolated,
        )
        self.assertNotEqual(isolated, Path(ROOT).resolve())
        self.assertFalse(
            isolated == self.root.resolve() or isolated == self.repo.resolve(),
            "isolated HOME must be distinct from the repo and its parent",
        )

    def test_outside_snapshot_includes_nested_team_leaf_on_repo_parent(self):
        keep = self._parent_team("keep.txt")
        nested = self._parent_team("work", "x")
        keep.parent.mkdir(parents=True)
        keep.write_text("keep\n", encoding="utf-8")
        nested.parent.mkdir(parents=True)
        nested.write_text("new\n", encoding="utf-8")
        snap = outside_snapshot(self.repo)
        self.assertTrue(
            _snap_keys_containing(snap, ".team/work/x"),
            "nested ../.team/work/x must be in the snapshot, got %s" % sorted(snap),
        )
        self.assertTrue(
            _snap_keys_containing(snap, ".team/keep.txt"),
            "pre-existing ../.team/keep.txt must be in the snapshot, got %s"
            % sorted(snap),
        )
        dir_only = [key for key in snap if key.replace("\\", "/").rstrip("/") == "../.team"]
        self.assertTrue(
            _snap_keys_containing(snap, ".team/work/x"),
            "a ../.team dir token alone is not the leaf: %s" % dir_only,
        )

    def test_outside_snapshot_includes_nested_team_leaf_on_home(self):
        self._assert_home_isolated()
        os.environ["TEAM_HOME"] = str(self.elsewhere)
        self.assertNotEqual(Path.home().resolve(), Path(self.elsewhere).resolve())
        nested = Path.home() / ".team" / "nested" / "y"
        nested.parent.mkdir(parents=True)
        nested.write_text("home-nested\n", encoding="utf-8")
        (self.elsewhere / ".team" / "ignored").mkdir(parents=True)
        (self.elsewhere / ".team" / "ignored" / "z").write_text("no\n", encoding="utf-8")
        snap = outside_snapshot(self.repo)
        self.assertTrue(
            _snap_keys_containing(snap, ".team/nested/y"),
            "home nested file must be in the snapshot (Path.home(), not TEAM_HOME): %s"
            % sorted(snap),
        )
        self.assertFalse(
            _snap_keys_containing(snap, ".team/ignored/z"),
            "TEAM_HOME is not an observer base: %s" % sorted(snap),
        )

    def test_outside_changed_sees_nested_create_when_dir_token_unchanged(self):
        team = self._parent_team()
        team.mkdir()
        keep = self._parent_team("keep.txt")
        keep.write_text("keep\n", encoding="utf-8")
        before = outside_snapshot(self.repo)
        nested = self._parent_team("work", "x")
        nested.parent.mkdir(parents=True)
        nested.write_text("new\n", encoding="utf-8")
        after = outside_snapshot(self.repo)
        if "../.team" in before and "../.team" in after:
            self.assertEqual(before.get("../.team"), after.get("../.team"))
        delta = outside_changed(before, after)
        self.assertTrue(delta, "nested create must change the snapshot")
        self.assertTrue(
            any(".team/work/x" in path.replace("\\", "/") for path in delta),
            "outside_changed must name the nested path, got %s" % delta,
        )

    def test_revert_outside_restores_nested_create_modify_delete(self):
        keep = self._parent_team("keep.txt")
        keep.parent.mkdir(parents=True)
        keep.write_text("keep\n", encoding="utf-8")
        before = {
            "outside": outside_snapshot(self.repo),
            "outside_blobs": outside_blobs(self.repo),
        }
        nested = self._parent_team("work", "x")
        nested.parent.mkdir(parents=True)
        nested.write_text("new\n", encoding="utf-8")
        keep.write_text("pwn\n", encoding="utf-8")
        revert_outside(self.repo, before)
        self.assertFalse(nested.exists(), "created nested file must be gone")
        self.assertEqual(keep.read_text(encoding="utf-8"), "keep\n")
        self.assertTrue(self._parent_team().is_dir(), "pre-existing .team remains")
        work_dir = self._parent_team("work")
        self.assertFalse(
            work_dir.exists(),
            "empty dir created only for the nested write must be removed",
        )

        keep.unlink()
        revert_outside(self.repo, before)
        self.assertTrue(keep.is_file())
        self.assertEqual(keep.read_text(encoding="utf-8"), "keep\n")

    def test_write_vibe_rc_on_repo_parent_is_fence_error_and_restored(self):
        self._assert_vibe_rc_restored(self._outside_cases())

    def test_write_outside_repo_is_fence_error_and_does_not_persist(self):
        self._assert_vibe_rc_restored(self._outside_cases())

    def test_write_vibe_rc_on_home_is_fence_error_and_restored(self):
        self._assert_home_isolated()
        os.environ["TEAM_HOME"] = str(self.elsewhere)
        home_vibe = Path.home() / "vibe.rc"
        self._assert_vibe_rc_restored(
            (("home-absolute", str(home_vibe), home_vibe),)
        )

    def _assert_vibe_rc_restored(self, cases):
        for label, rel, actual in cases:
            with self.subTest(encoding=label):
                if actual.exists():
                    actual.unlink()
                actual.write_text("user-outside\n", encoding="utf-8")
                pipe = self._pipe(slug="out-%s" % label.replace("/", "-"))
                head_before = head_sha(self.repo)
                pre_keep = (self.repo / "src" / "keep.py").read_text(encoding="utf-8")
                hostile = HostileRuntime(
                    [
                        write(rel, "pwned-outside\n"),
                        emit(WRITE_SUMMARY([rel])),
                    ],
                    phases=("implementer",),
                )
                with register_runtime("fake", hostile):
                    with self.assertRaises(PipelineError) as ctx:
                        pipe.phase_implementer()
                self.assertTrue(
                    "vibe.rc" in str(ctx.exception) or rel in str(ctx.exception),
                    ctx.exception,
                )
                self.assertEqual(actual.read_text(encoding="utf-8"), "user-outside\n")
                self.assertNotIn("pwned-outside", actual.read_text(encoding="utf-8"))
                self.assertEqual(head_sha(self.repo), head_before)
                self.assertEqual(
                    (self.repo / "src" / "keep.py").read_text(encoding="utf-8"),
                    pre_keep,
                )
                actual.unlink()

    def test_implementer_nested_team_on_parent_is_fence_error_and_does_not_persist(self):
        self._parent_team().mkdir(exist_ok=True)
        keep = self._parent_team("keep.txt")
        keep.write_text("keep\n", encoding="utf-8")
        nested = self._parent_team("work", "x")
        rel = "../.team/work/x"
        abs_rel = str(nested)
        for code_root in ("src", "."):
            with self.subTest(code_root=code_root):
                if nested.exists():
                    nested.unlink()
                pipe = self._pipe(
                    slug="team-parent-%s" % ("dot" if code_root == "." else "src"),
                    code_root=code_root,
                )
                head_before = head_sha(self.repo)
                pre_keep = (self.repo / "src" / "keep.py").read_text(encoding="utf-8")
                hostile = HostileRuntime(
                    [
                        write(rel, "pwned-team\n"),
                        write(abs_rel, "pwned-team\n"),
                        emit(WRITE_SUMMARY([rel])),
                    ],
                    phases=("implementer",),
                )
                with register_runtime("fake", hostile):
                    with self.assertRaises(PipelineError) as ctx:
                        pipe.phase_implementer()
                msg = str(ctx.exception)
                self.assertTrue(
                    ".team" in msg or "work/x" in msg or rel in msg,
                    ctx.exception,
                )
                self.assertFalse(nested.exists(), "nested extra-worktree write must not persist")
                self.assertEqual(head_sha(self.repo), head_before)
                self.assertEqual(
                    (self.repo / "src" / "keep.py").read_text(encoding="utf-8"),
                    pre_keep,
                )
                self.assertEqual(keep.read_text(encoding="utf-8"), "keep\n")

    def test_readonly_hop_nested_team_is_fence_error_and_does_not_persist(self):
        self._parent_team().mkdir(exist_ok=True)
        nested = self._parent_team("work", "x")
        if nested.exists():
            nested.unlink()
        pipe = self._pipe(slug="inspect-team")
        head_before = head_sha(self.repo)
        review_out = {"summary": "ok", "findings": [], "review_markdown": "ok"}
        hostile = HostileRuntime(
            [
                write("../.team/work/x", "pwned-inspect\n"),
                emit(review_out),
            ],
            phases=("reviewer-fake",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_reviewer()
        msg = str(ctx.exception)
        self.assertTrue(
            ".team" in msg or "work/x" in msg or "../.team/work/x" in msg,
            ctx.exception,
        )
        self.assertFalse(nested.exists(), "inspect restore must evaluate the directory leaf")
        self.assertEqual(head_sha(self.repo), head_before)

    def test_implementer_nested_team_on_home_is_fence_error_and_does_not_persist(self):
        self._assert_home_isolated()
        os.environ["TEAM_HOME"] = str(self.elsewhere)
        nested = Path.home() / ".team" / "nested" / "y"
        if nested.exists():
            nested.unlink()
        pipe = self._pipe(slug="team-home")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [
                write(str(nested), "pwned-home\n"),
                emit(WRITE_SUMMARY([str(nested)])),
            ],
            phases=("implementer",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        msg = str(ctx.exception)
        self.assertTrue(".team" in msg or "nested/y" in msg or str(nested) in msg, ctx.exception)
        self.assertFalse(nested.exists(), "home nested extra-worktree write must not persist")
        self.assertEqual(head_sha(self.repo), head_before)
        self.assertFalse(
            (self.elsewhere / ".team" / "nested" / "y").exists(),
            "nothing created under TEAM_HOME is the property",
        )


class DesignRootAdoptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unlocked_replan_replaces_stale_state_src(self):
        cfg = load_config(self.repo, fake=True, force=True)
        self.assertFalse(cfg.lock_code_root)
        pipe = start_feature(cfg, "fence brief", "adopt")
        pipe.cfg.code_root = "src"
        pipe.state.code_root = "src"
        pipe._adopt_design_roots({"code_root": ".", "test_root": "tests/"})
        self.assertEqual(pipe.cfg.code_root, ".")
        self.assertEqual(pipe.cfg.test_root, "tests")
        self.assertEqual(pipe.state.code_root, ".")
        self.assertEqual(pipe.state.test_root, "tests")

    def test_config_locked_root_is_not_replaced(self):
        cfg = load_config(
            self.repo, fake=True, force=True, code_root="src", test_root="tests"
        )
        self.assertTrue(cfg.lock_code_root)
        pipe = start_feature(cfg, "fence brief", "locked")
        pipe._adopt_design_roots({"code_root": ".", "test_root": "other/"})
        self.assertEqual(pipe.cfg.code_root, "src")
        self.assertEqual(pipe.cfg.test_root, "tests")

    def test_replan_hop_adopts_dot_from_schema(self):
        cfg = load_config(self.repo, fake=True, force=True)
        pipe = start_feature(cfg, "fence brief", "replan-dot")
        pipe.cfg.code_root = "src"
        pipe.state.code_root = "src"
        pipe.write_artifact("review.md", "# Review\n")
        pipe.write_artifact("design.md", "# Design\n")
        questions = emit({"questions_for_tdd": [], "questions_for_implementer": []})
        design = emit(
            {
                "design_markdown": "# Delta\n\nThis slug's apply fence is code_root='.'\n",
                "code_root": ".",
                "test_root": "tests/",
                "acceptance_criteria": [],
                "structural_touchpoints": [],
                "invariants": [],
            }
        )
        hostile = HostileRuntime(
            by_phase={"replan-questions": [questions], "replan": [design]}
        )
        with register_runtime("fake", hostile):
            pipe.replan()
        self.assertEqual(pipe.cfg.code_root, ".")
        self.assertEqual(pipe.cfg.test_root, "tests")
        reloaded = load_pipeline(
            load_config(self.repo, fake=True, force=True), "replan-dot"
        )
        self.assertEqual(reloaded.cfg.code_root, ".")
        self.assertFalse(reloaded.cfg.lock_code_root)

    def test_apply_design_delta_adopts_replan_artifact_roots(self):
        """Merging design-replan.md must move the fence before later hops."""
        cfg = load_config(self.repo, fake=True, force=True)
        pipe = start_feature(cfg, "fence brief", "delta-adopt")
        pipe.cfg.code_root = "src"
        pipe.state.code_root = "src"
        pipe.write_artifact("design.md", "# Design\n\nOld fence was src.\n")
        pipe.write_artifact(
            "design-replan.md",
            "# Delta\n\nThis slug's apply fence is code_root='.'\n",
        )
        dump_json(
            pipe.work / "prompts" / "replan.result.json",
            {
                "design_markdown": "# Delta\n",
                "code_root": ".",
                "test_root": "tests/",
            },
        )
        pipe._apply_design_delta()
        self.assertEqual(pipe.cfg.code_root, ".")
        self.assertEqual(pipe.state.code_root, ".")
        self.assertEqual(pipe.cfg.test_root, "tests")
        reloaded = State.load(pipe.work)
        self.assertEqual(reloaded.code_root, ".")
        self.assertEqual(reloaded.test_root, "tests")


if __name__ == "__main__":
    unittest.main()
