"""Contract tests for the review-since-tag architecture delta.

Membership is resolved capability and the named seams — not a first-hop
phase list and not a regex census of invoke() strings.
"""

from __future__ import annotations

import ast
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.config import load_config, schema_path
from team.gitutil import head as git_head
from team.pipeline import PipelineError, start_audit, start_feature
from team.schemas import validate as validate_schema
from team.util import load_json
from tests.support.hostile import (
    HostileRuntime,
    commit,
    crash,
    delete,
    emit,
    register_runtime,
    rename,
    write,
)
from tests.support.repo import git, head_sha, init_repo


KEEP = "keep\n"
PWNED = "pwned-by-inspect\n"

DESIGN_BODY = {
    "design_markdown": "# Design\n",
    "code_root": "src",
    "test_root": "tests",
    "acceptance_criteria": [],
    "structural_touchpoints": [],
    "invariants": [],
}

GATE_READY = {
    "ready": True,
    "consult": "none",
    "questions": [],
    "summary": "ready",
}

TDD_READY = {
    "ready": True,
    "questions": [],
    "test_contract_markdown": "# Contract\n",
    "criteria_map": [],
}

STATUS_BODY = {
    "status_markdown": "# Status\n",
    "summary": "ok",
}

ANSWERS_BODY = {"answers_markdown": "proceed"}

WRITE_SUMMARY = {"summary": "hostile", "paths_touched": ["src/greet.py"]}

FIRST_HOP_CHECKLIST = (
    "reviewer",
    "guardian",
    "critic",
    "architect",
    "tdd-design",
    "debugger",
    "seq-reviewer",
    "seq-guardian",
)


def _keep_bytes(repo: Path) -> str:
    path = repo / "src" / "keep.py"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


class SeededRepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "keep.py").write_text(KEEP, encoding="utf-8")
        (self.repo / "tests" / "test_a.py").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "--", "src/keep.py", "tests/test_a.py")
        git(self.repo, "commit", "-m", "seed roots")

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, code_root="src", test_root="tests", test_command=""):
        return load_config(
            self.repo,
            fake=True,
            force=True,
            code_root=code_root,
            test_root=test_root,
            test_command=test_command,
        )

    def _pipe(self, slug="delta", **cfg_kw):
        return start_feature(self._cfg(**cfg_kw), "delta brief", slug)

    def _assert_tree_restored(self, head_before: str) -> None:
        self.assertTrue((self.repo / "src" / "keep.py").is_file())
        self.assertEqual(_keep_bytes(self.repo), KEEP)
        self.assertEqual(head_sha(self.repo), head_before)
        self.assertEqual(git_head(self.repo), head_before)

    def _hostile_readonly(self, actions, **kwargs):
        kwargs.setdefault("num_turns", 2)
        return HostileRuntime(actions, **kwargs)


class ReadOnlyFenceMembershipTests(SeededRepoTests):
    def test_invoke_read_only_fences_unlisted_phase(self):
        """Capability membership, not the first-hop checklist."""
        self.assertNotIn("future-readonly", FIRST_HOP_CHECKLIST)
        pipe = self._pipe(slug="unlisted")
        head_before = head_sha(self.repo)
        hostile = self._hostile_readonly(
            [write("src/keep.py", PWNED), emit(DESIGN_BODY)],
            phases=("future-readonly",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.invoke(
                    "architect",
                    "future-readonly",
                    "do not edit",
                    "design.json",
                )
        self.assertIn("src/keep.py", str(ctx.exception))
        self._assert_tree_restored(head_before)
        self.assertEqual(
            [c["capability"] for c in hostile.calls if c["phase"] == "future-readonly"],
            ["read-only"],
        )

    def test_read_only_override_fences_write_capable_role(self):
        pipe = self._pipe(slug="override")
        head_before = head_sha(self.repo)
        consult_rt = self._hostile_readonly(
            [write("src/keep.py", PWNED), emit(ANSWERS_BODY)],
        )
        with register_runtime("fake", consult_rt):
            with self.assertRaises(PipelineError):
                pipe.consult("implementer", ["why?"], from_role="architect")
        self._assert_tree_restored(head_before)
        consult_caps = [c["capability"] for c in consult_rt.calls if c["phase"].startswith("consult")]
        self.assertTrue(consult_caps)
        self.assertEqual(set(consult_caps), {"read-only"})
        self.assertNotIn("write-code", consult_caps)

        for role, phase, schema in (
            ("implementer", "implementer-gate", "gate.json"),
            ("test-writer", "test-writer-gate", "gate.json"),
        ):
            with self.subTest(phase=phase):
                head_before = head_sha(self.repo)
                (self.repo / "src" / "keep.py").write_text(KEEP, encoding="utf-8")
                git(self.repo, "checkout", "HEAD", "--", "src/keep.py")
                hostile = self._hostile_readonly(
                    [write("src/pwned.py", "x\n"), emit(GATE_READY)],
                    phases=(phase,),
                )
                with register_runtime("fake", hostile):
                    with self.assertRaises(PipelineError):
                        pipe.invoke(
                            role,
                            phase,
                            "gate only",
                            schema,
                            capability="read-only",
                        )
                self.assertFalse((self.repo / "src" / "pwned.py").exists())
                self._assert_tree_restored(head_before)
                self.assertEqual(
                    {c["capability"] for c in hostile.calls if c["phase"] == phase},
                    {"read-only"},
                )

    def test_non_write_witness_hops_restore_product_writes(self):
        """Witnesses of hops that escaped the per-phase checklist. Not an allowlist."""
        cases = (
            (
                "architect-revise",
                lambda: self._pipe(slug="follow-revise"),
                lambda pipe: pipe.phase_critic(),
                {
                    "critic": [
                        emit(
                            {
                                "accepts": False,
                                "issues": ["brief uncovered"],
                                "attacks": [],
                                "critic_markdown": "reject",
                            }
                        )
                    ],
                    "architect-revise": [write("src/keep.py", PWNED), emit(DESIGN_BODY)],
                },
            ),
            (
                "tdd-design-write",
                lambda: self._pipe(slug="follow-tdd-write"),
                lambda pipe: pipe.phase_tdd_design(),
                {
                    "tdd-design": [
                        emit(
                            {
                                "ready": False,
                                "questions": ["what is greet?"],
                                "test_contract_markdown": "",
                                "criteria_map": [],
                            }
                        )
                    ],
                    "consult-001": [emit(ANSWERS_BODY)],
                    "tdd-design-write": [write("src/keep.py", PWNED), emit(TDD_READY)],
                },
            ),
            (
                "tdd-design-apply-write",
                lambda: self._pipe(slug="follow-tdd-apply"),
                lambda pipe: pipe._apply_tdd_design(
                    [{"kind": "test", "title": "x", "path": "tests/test_a.py"}]
                ),
                {
                    "tdd-design-apply": [
                        emit(
                            {
                                "ready": False,
                                "questions": ["clarify criterion"],
                                "test_contract_markdown": "",
                                "criteria_map": [],
                            }
                        )
                    ],
                    "consult-001": [emit(ANSWERS_BODY)],
                    "tdd-design-apply-write": [
                        write("src/keep.py", PWNED),
                        emit(TDD_READY),
                    ],
                },
            ),
            (
                "consult",
                lambda: self._pipe(slug="follow-consult"),
                lambda pipe: pipe.consult("architect", ["q"], from_role="tdd-design"),
                {"consult-001": [write("src/keep.py", PWNED), emit(ANSWERS_BODY)]},
            ),
            (
                "assess",
                lambda: start_audit(self._cfg(), "status?", "follow-assess"),
                lambda pipe: pipe.phase_assess(),
                {"assess": [write("src/keep.py", PWNED), emit(STATUS_BODY)]},
            ),
            (
                "test-writer-gate",
                lambda: self._pipe(slug="follow-tw-gate"),
                lambda pipe: pipe.invoke(
                    "test-writer",
                    "test-writer-gate",
                    "gate only",
                    "gate.json",
                    capability="read-only",
                ),
                {"test-writer-gate": [write("src/keep.py", PWNED), emit(GATE_READY)]},
            ),
            (
                "implementer-gate",
                lambda: self._pipe(slug="follow-impl-gate"),
                lambda pipe: pipe.invoke(
                    "implementer",
                    "implementer-gate",
                    "gate only",
                    "gate.json",
                    capability="read-only",
                ),
                {"implementer-gate": [write("src/keep.py", PWNED), emit(GATE_READY)]},
            ),
            (
                "inspect-retry",
                lambda: self._pipe(slug="follow-retry"),
                lambda pipe: pipe.invoke(
                    "architect",
                    "architect",
                    "inspect first",
                    "design.json",
                ),
                {"architect": [write("src/keep.py", PWNED), emit(DESIGN_BODY)]},
            ),
        )
        for name, make_pipe, run, scripts in cases:
            with self.subTest(hop=name):
                git(self.repo, "checkout", "HEAD", "--", "src/keep.py")
                (self.repo / "src" / "keep.py").write_text(KEEP, encoding="utf-8")
                pipe = make_pipe()
                head_before = head_sha(self.repo)
                num_turns = 1 if name == "inspect-retry" else 2
                hostile = self._hostile_readonly(None, by_phase=scripts, num_turns=num_turns)
                with register_runtime("fake", hostile):
                    with self.assertRaises(PipelineError):
                        run(pipe)
                self._assert_tree_restored(head_before)

    def test_invoke_fence_membership_is_may_write(self):
        src = (ROOT / "src" / "team" / "pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn(
            'cap in ("read-only", "write-code", "write-tests")',
            src,
        )
        self.assertIn("may_write", src)
        self.assertIn("not may_write", src)

    test_unlisted_phase_name_is_fenced_without_a_new_verify_site = (
        test_invoke_read_only_fences_unlisted_phase
    )

    def test_runtime_complete_has_one_production_caller(self):
        callers = []
        for path in (ROOT / "src" / "team").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name == "complete":
                    continue
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "complete"
                    ):
                        callers.append((path.relative_to(ROOT).as_posix(), node.name, child.lineno))
        self.assertEqual(
            {(fn, name) for fn, name, _ in callers},
            {("src/team/pipeline.py", "invoke")},
            "Runtime.complete must have one production caller (invoke); got %s" % callers,
        )


class ReadOnlyRestoreTests(SeededRepoTests):
    def _invoke_hostile(self, actions):
        pipe = self._pipe(slug="restore")
        head_before = head_sha(self.repo)
        hostile = self._hostile_readonly(actions, phases=("future-readonly",))
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.invoke(
                    "architect",
                    "future-readonly",
                    "hostile",
                    "design.json",
                    capability="read-only",
                )
        return pipe, head_before, ctx.exception

    def test_readonly_modify_restores_pre_hop_bytes(self):
        """Unlink-as-restore must fail this case."""
        _, head_before, exc = self._invoke_hostile(
            [write("src/keep.py", PWNED), emit(DESIGN_BODY)]
        )
        self.assertIn("src/keep.py", str(exc))
        keep = self.repo / "src" / "keep.py"
        self.assertTrue(keep.is_file(), "restore must not unlink a modified tracked file")
        self.assertEqual(keep.read_text(encoding="utf-8"), KEEP)
        self.assertNotEqual(keep.read_text(encoding="utf-8"), PWNED)
        self.assertEqual(head_sha(self.repo), head_before)

    def test_readonly_delete_restores_tracked_file(self):
        _, head_before, exc = self._invoke_hostile(
            [delete("src/keep.py"), emit(DESIGN_BODY)]
        )
        self.assertTrue(isinstance(exc, PipelineError))
        keep = self.repo / "src" / "keep.py"
        self.assertTrue(keep.is_file(), "unlink-only restore is a no-op on a deleted path")
        self.assertEqual(keep.read_text(encoding="utf-8"), KEEP)
        self.assertEqual(head_sha(self.repo), head_before)

    def test_readonly_create_is_gone(self):
        _, head_before, _exc = self._invoke_hostile(
            [write("src/pwned.py", "x\n"), emit(DESIGN_BODY)]
        )
        self.assertFalse((self.repo / "src" / "pwned.py").exists())
        self.assertEqual(_keep_bytes(self.repo), KEEP)
        self.assertEqual(head_sha(self.repo), head_before)

    def test_readonly_commit_restores_head_and_tree(self):
        _, head_before, _exc = self._invoke_hostile(
            [
                write("src/keep.py", PWNED),
                commit("hostile inspect", ["src/keep.py"]),
                emit(DESIGN_BODY),
            ]
        )
        self.assertEqual(head_sha(self.repo), head_before)
        self.assertEqual(_keep_bytes(self.repo), KEEP)
        self.assertFalse((self.repo / "src" / "pwned.py").exists())

    def test_readonly_shapes_restore_pre_hop_tree_and_head(self):
        shapes = (
            ([write("src/keep.py", PWNED), emit(DESIGN_BODY)], "src/keep.py", KEEP, False, None),
            ([delete("src/keep.py"), emit(DESIGN_BODY)], "src/keep.py", KEEP, False, None),
            ([write("src/pwned.py", "x\n"), emit(DESIGN_BODY)], "src/pwned.py", None, True, None),
            (
                [
                    write("src/keep.py", PWNED),
                    commit("hostile inspect", ["src/keep.py"]),
                    emit(DESIGN_BODY),
                ],
                "src/keep.py",
                KEEP,
                False,
                None,
            ),
            (
                [rename("src/keep.py", "src/stolen.py"), emit(DESIGN_BODY)],
                "src/keep.py",
                KEEP,
                False,
                "src/stolen.py",
            ),
        )
        for actions, rel, pre, created, dest in shapes:
            with self.subTest(rel=rel, created=created, dest=dest):
                git(self.repo, "checkout", "HEAD", "--", "src/keep.py")
                (self.repo / "src" / "keep.py").write_text(KEEP, encoding="utf-8")
                if (self.repo / "src" / "pwned.py").exists():
                    (self.repo / "src" / "pwned.py").unlink()
                if (self.repo / "src" / "stolen.py").exists():
                    git(self.repo, "checkout", "HEAD", "--", "src/keep.py")
                    if (self.repo / "src" / "stolen.py").exists():
                        (self.repo / "src" / "stolen.py").unlink()
                _, head_before, exc = self._invoke_hostile(actions)
                self.assertIsInstance(exc, PipelineError)
                path = self.repo / rel
                self.assertEqual(head_sha(self.repo), head_before)
                if created:
                    self.assertFalse(path.exists())
                else:
                    self.assertTrue(path.is_file())
                    self.assertEqual(path.read_text(encoding="utf-8"), pre)
                if dest:
                    self.assertFalse((self.repo / dest).exists())

    def test_readonly_rename_restores_source_and_drops_dest(self):
        _, head_before, exc = self._invoke_hostile(
            [rename("src/keep.py", "src/stolen.py"), emit(DESIGN_BODY)]
        )
        self.assertIn("src/keep.py", str(exc))
        keep = self.repo / "src" / "keep.py"
        self.assertTrue(keep.is_file())
        self.assertEqual(keep.read_text(encoding="utf-8"), KEEP)
        self.assertFalse((self.repo / "src" / "stolen.py").exists())
        self.assertEqual(head_sha(self.repo), head_before)

    def test_readonly_mixed_shapes_revert_together(self):
        _, head_before, _exc = self._invoke_hostile(
            [
                write("src/keep.py", PWNED),
                delete("tests/test_a.py"),
                write("src/new.py", "new\n"),
                commit("mixed hostile", None),
                emit(DESIGN_BODY),
            ]
        )
        self.assertEqual(head_sha(self.repo), head_before)
        self.assertEqual(_keep_bytes(self.repo), KEEP)
        self.assertTrue((self.repo / "tests" / "test_a.py").is_file())
        self.assertEqual(
            (self.repo / "tests" / "test_a.py").read_text(encoding="utf-8"), "ok\n"
        )
        self.assertFalse((self.repo / "src" / "new.py").exists())

    def test_restore_skips_team_work(self):
        scratch = ".team/work/restore/agent-scratch.md"
        pipe, head_before, _exc = self._invoke_hostile(
            [
                write("src/keep.py", PWNED),
                write(scratch, "agent notes\n"),
                emit(DESIGN_BODY),
            ]
        )
        self._assert_tree_restored(head_before)
        result_json = pipe.work / "prompts" / "future-readonly.result.json"
        self.assertTrue(
            result_json.is_file() or (self.repo / scratch).is_file(),
            "restore must not be required to delete .team/work",
        )


class AlreadyDirtyNotReadOnlyGrantTests(SeededRepoTests):
    USER_WIP = "user-wip\n"

    def _pipe_with_dirty_keep(self, slug):
        (self.repo / "src" / "keep.py").write_text(self.USER_WIP, encoding="utf-8")
        return self._pipe(slug=slug)

    def _assert_wip_restored(self, head_before: str) -> None:
        keep = self.repo / "src" / "keep.py"
        self.assertTrue(keep.is_file())
        self.assertEqual(keep.read_text(encoding="utf-8"), self.USER_WIP)
        self.assertNotEqual(keep.read_text(encoding="utf-8"), KEEP)
        self.assertNotEqual(keep.read_text(encoding="utf-8"), PWNED)
        self.assertEqual(head_sha(self.repo), head_before)

    def test_read_only_already_dirty_product_write_is_restored(self):
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
        debugger_out = {
            "owner": "implementer",
            "root_cause": "x",
            "diagnosis_markdown": "x",
            "disposition": "retry",
        }
        cases = []

        def add(name, make_pipe, run, scripts):
            cases.append((name, make_pipe, run, scripts))

        add(
            "unlisted",
            lambda: self._pipe_with_dirty_keep("ro-dirty-unlisted"),
            lambda pipe: pipe.invoke(
                "architect",
                "future-readonly",
                "do not edit",
                "design.json",
                capability="read-only",
            ),
            {"future-readonly": [write("src/keep.py", PWNED), emit(DESIGN_BODY)]},
        )
        add(
            "implementer-gate",
            lambda: self._pipe_with_dirty_keep("ro-dirty-ig"),
            lambda pipe: pipe.invoke(
                "implementer",
                "implementer-gate",
                "gate",
                "gate.json",
                capability="read-only",
            ),
            {"implementer-gate": [write("src/keep.py", PWNED), emit(GATE_READY)]},
        )
        add(
            "test-writer-gate",
            lambda: self._pipe_with_dirty_keep("ro-dirty-tg"),
            lambda pipe: pipe.invoke(
                "test-writer",
                "test-writer-gate",
                "gate",
                "gate.json",
                capability="read-only",
            ),
            {"test-writer-gate": [write("src/keep.py", PWNED), emit(GATE_READY)]},
        )
        add(
            "consult",
            lambda: self._pipe_with_dirty_keep("ro-dirty-consult"),
            lambda pipe: pipe.consult("architect", ["q"], from_role="implementer"),
            {"consult-001": [write("src/keep.py", PWNED), emit(ANSWERS_BODY)]},
        )
        add(
            "reviewer",
            lambda: self._pipe_with_dirty_keep("ro-dirty-rev"),
            lambda pipe: pipe.phase_reviewer(),
            {"reviewer-fake": [write("src/keep.py", PWNED), emit(review_out)]},
        )
        add(
            "architect",
            lambda: self._pipe_with_dirty_keep("ro-dirty-arch"),
            lambda pipe: pipe.phase_architect(),
            {"architect": [write("src/keep.py", PWNED), emit(DESIGN_BODY)]},
        )
        add(
            "debugger",
            lambda: self._pipe_with_dirty_keep("ro-dirty-dbg"),
            lambda pipe: (
                setattr(pipe.state, "final", {"status": "FAIL", "failing": []}),
                pipe.phase_debugger(),
            )[-1],
            {"debugger": [write("src/keep.py", PWNED), emit(debugger_out)]},
        )
        add(
            "seq-reviewer",
            lambda: self._pipe_with_dirty_keep("ro-dirty-seqr"),
            lambda pipe: pipe.phase_seq_review(
                pipe.work / "seq" / "deadbeefcafe",
                [{"kind": "test", "title": "x"}],
            ),
            {"seq-reviewer-fake": [write("src/keep.py", PWNED), emit(review_out)]},
        )
        add(
            "seq-guardian",
            lambda: self._pipe_with_dirty_keep("ro-dirty-seqg"),
            lambda pipe: pipe._phase_seq_guardian(
                pipe.work / "seq" / "deadbeefcafe",
                [{"kind": "test", "title": "x"}],
            ),
            {"seq-guardian": [write("src/keep.py", PWNED), emit(guardian_out)]},
        )

        for name, make_pipe, run, scripts in cases:
            with self.subTest(hop=name):
                git(self.repo, "checkout", "HEAD", "--", "src/keep.py")
                pipe = make_pipe()
                if name.startswith("seq-"):
                    (pipe.work / "seq" / "deadbeefcafe" / "prompts").mkdir(parents=True)
                head_before = head_sha(self.repo)
                hostile = self._hostile_readonly(None, by_phase=scripts)
                with register_runtime("fake", hostile):
                    with self.assertRaises(PipelineError) as ctx:
                        run(pipe)
                self.assertIn("src/keep.py", str(ctx.exception))
                self._assert_wip_restored(head_before)

    test_read_only_already_dirty_wip_restores_wip_bytes_not_head = (
        test_read_only_already_dirty_product_write_is_restored
    )

    def test_unrelated_dirty_path_is_untouched(self):
        (self.repo / "NOTES").write_text("scratch\n", encoding="utf-8")
        pipe = self._pipe(slug="notes-untouched")
        head_before = head_sha(self.repo)
        hostile = self._hostile_readonly(
            [write("src/keep.py", PWNED), emit(DESIGN_BODY)],
            phases=("future-readonly",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError):
                pipe.invoke(
                    "architect",
                    "future-readonly",
                    "hostile",
                    "design.json",
                    capability="read-only",
                )
        self.assertEqual(
            (self.repo / "NOTES").read_text(encoding="utf-8"), "scratch\n"
        )
        keep = self.repo / "src" / "keep.py"
        self.assertEqual(keep.read_text(encoding="utf-8"), KEEP)
        self.assertEqual(head_sha(self.repo), head_before)
        verify = pipe.work / "git" / "verify-future-readonly.md"
        if verify.is_file():
            self.assertNotIn("NOTES", verify.read_text(encoding="utf-8"))


class CrashAfterWriteRestoreTests(SeededRepoTests):
    def test_complete_crash_after_product_write_still_restores(self):
        pipe = self._pipe(slug="crash-ro")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [write("src/keep.py", PWNED), crash(1, "boom")],
            phases=("future-readonly",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError):
                pipe.invoke(
                    "architect",
                    "future-readonly",
                    "crash",
                    "design.json",
                    capability="read-only",
                )
        self._assert_tree_restored(head_before)

        (self.repo / "src" / "keep.py").write_text("user-wip\n", encoding="utf-8")
        pipe = self._pipe(slug="crash-wip")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [write("src/keep.py", PWNED), crash(1, "boom")],
            phases=("future-readonly",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError):
                pipe.invoke(
                    "architect",
                    "future-readonly",
                    "crash",
                    "design.json",
                    capability="read-only",
                )
        self.assertEqual(
            (self.repo / "src" / "keep.py").read_text(encoding="utf-8"), "user-wip\n"
        )
        self.assertEqual(head_sha(self.repo), head_before)

        git(self.repo, "checkout", "HEAD", "--", "src/keep.py")
        (self.repo / "src" / "keep.py").write_text(KEEP, encoding="utf-8")
        pipe = self._pipe(slug="crash-oor")
        head_before = head_sha(self.repo)
        pre = (self.repo / "tests" / "test_a.py").read_text(encoding="utf-8")
        hostile = HostileRuntime(
            [write("tests/test_a.py", "pwned-oor\n"), crash(1, "boom")],
            phases=("implementer",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(Exception):
                pipe.phase_implementer()
        self.assertEqual(
            (self.repo / "tests" / "test_a.py").read_text(encoding="utf-8"), pre
        )
        self.assertEqual(head_sha(self.repo), head_before)

    def test_schema_invalid_after_product_write_still_restores(self):
        pipe = self._pipe(slug="schema-ro")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [write("src/keep.py", PWNED), emit({"summary": "no findings key"})],
            phases=("reviewer-fake",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError):
                pipe.phase_reviewer()
        self._assert_tree_restored(head_before)
        self.assertTrue((pipe.work / "prompts" / "reviewer-fake.result.json").is_file())


class WriteCapabilityNotReadonlyTests(SeededRepoTests):
    def test_write_code_in_root_persists(self):
        pipe = self._pipe(slug="write-ok")
        hostile = HostileRuntime(
            [
                write("src/greet.py", "def greet():\n    return 'hello'\n"),
                emit(WRITE_SUMMARY),
            ],
            phases=("implementer",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            pipe.phase_implementer()
        greet = self.repo / "src" / "greet.py"
        self.assertTrue(greet.is_file())
        self.assertIn("hello", greet.read_text(encoding="utf-8"))
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertNotIn("violations:", verify)

    def test_write_capability_outside_root_is_outside_root_error(self):
        pipe = self._pipe(slug="write-out")
        hostile = HostileRuntime(
            [write("tests/test_a.py", "nope\n"), emit(WRITE_SUMMARY)],
            phases=("implementer",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        msg = str(ctx.exception)
        self.assertIn("tests/test_a.py", msg)
        lowered = msg.lower()
        self.assertTrue(
            "outside" in lowered or "allowed" in lowered or "wrote" in lowered,
            "write-capability violation is the outside-root class, got %s" % msg,
        )
        self.assertNotIn("tree changed", lowered)
        self.assertNotIn("inspect restore", lowered)

        tw = HostileRuntime(
            [write("src/keep.py", PWNED), emit({"summary": "x", "paths_touched": ["src/keep.py"]})],
            phases=("test-writer",),
            num_turns=2,
        )
        with register_runtime("fake", tw):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_test_writer()
        self.assertIn("src/keep.py", str(ctx.exception))
        self.assertIn("outside", str(ctx.exception).lower())

    def test_execute_is_not_readonly_restored(self):
        pipe = self._pipe(slug="exec")
        with self.assertRaises(PipelineError) as ctx:
            pipe.invoke("tester", "tester", "host only", "tester.json", runtime_name="host")
        self.assertIn("host-only", str(ctx.exception).lower())

    def test_execute_product_write_is_restored_and_raised(self):
        """Tester/execute has a shell, not a write license."""
        pipe = self._pipe(slug="exec-write")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [
                write("src/keep.py", PWNED),
                emit(
                    {
                        "passed": True,
                        "report_markdown": "ok",
                        "command_used": "true",
                    }
                ),
            ],
            phases=("tester",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.invoke(
                    "tester",
                    "tester",
                    "comment only",
                    "tester.json",
                    capability="execute",
                    runtime_name="fake",
                )
        self.assertIn("src/keep.py", str(ctx.exception))
        self._assert_tree_restored(head_before)


class HostArtifactIntervalTests(SeededRepoTests):
    def test_host_artifact_after_clean_readonly_invoke_is_ok(self):
        pipe = self._pipe(slug="host-ok")
        head_before = head_sha(self.repo)
        pipe.phase_architect()
        self.assertTrue((pipe.work / "design.md").is_file())
        self.assertTrue((pipe.work / "prompts" / "architect.result.json").is_file())
        self.assertEqual(_keep_bytes(self.repo), KEEP)
        self.assertEqual(head_sha(self.repo), head_before)

    def test_product_write_during_complete_is_still_a_hop_violation(self):
        pipe = self._pipe(slug="host-vs-hop")
        head_before = head_sha(self.repo)
        hostile = self._hostile_readonly(
            [write("src/keep.py", PWNED), emit(DESIGN_BODY)],
            phases=("architect",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError):
                pipe.phase_architect()
        self._assert_tree_restored(head_before)
        self.assertTrue((pipe.work / "prompts" / "architect.result.json").is_file())


class InspectTurnSetTests(SeededRepoTests):
    def test_finished_shaped_missing_turns_rejected_for_inspect_roles(self):
        """Progress-phrase-only tests do not evaluate this property."""
        finished = {
            "reviewer": (
                "review.json",
                {
                    "summary": "Range is complete; findings below.",
                    "findings": [
                        {
                            "severity": "low",
                            "title": "Residual note on docs",
                            "evidence": "README line 1",
                            "kind": "note",
                        }
                    ],
                    "review_markdown": "Finished review of the collected range.",
                },
            ),
            "guardian": (
                "guardian.json",
                {
                    "risks": [],
                    "guardian_markdown": "Chain evaluated after reading the tree.",
                    "chain": {
                        "r_to_a": {"ok": True, "note": "n"},
                        "a_to_t": {"ok": True, "note": "n"},
                        "t_to_i": {"ok": True, "note": "n"},
                        "i_to_r": {"ok": True, "note": "n"},
                    },
                },
            ),
            "scout": (
                "scout.json",
                {
                    "roots": ["."],
                    "components": [
                        {
                            "name": "readme",
                            "path": "README",
                            "state": "done",
                            "evidence": "README exists",
                        }
                    ],
                    "notes": "inventory complete",
                },
            ),
            "critic": (
                "critic.json",
                {
                    "accepts": True,
                    "issues": [],
                    "attacks": [],
                    "critic_markdown": "Design survived.",
                },
            ),
            "debugger": (
                "debugger.json",
                {
                    "owner": "implementer",
                    "root_cause": "x",
                    "diagnosis_markdown": "Inspected the failing suite and the impl.",
                    "disposition": "retry",
                },
            ),
        }
        pipe = self._pipe(slug="finished-missing-turns")
        for role, (schema, body) in finished.items():
            with self.subTest(role=role):
                phase = "reviewer-fake" if role == "reviewer" else role
                hostile = HostileRuntime([emit(body)], phases=(phase,), num_turns=None)
                with register_runtime("fake", hostile):
                    with self.assertRaises(PipelineError) as ctx:
                        pipe.invoke(role, phase, "inspect the tree", schema, runtime_name="fake")
                msg = str(ctx.exception).lower()
                self.assertTrue(
                    "inspect" in msg or "unfinished" in msg or "turn" in msg,
                    msg,
                )
                self.assertGreaterEqual(
                    len([c for c in hostile.calls if c["phase"] == phase]),
                    1,
                )
                self.assertNotIn("success", msg)

    def test_inspect_roles_are_not_derived_from_read_only_capability(self):
        pipe = self._pipe(slug="inspect-set")
        bodies = {
            ("architect", "architect", "design.json"): DESIGN_BODY,
            ("tdd-design", "tdd-design", "tdd_design.json"): TDD_READY,
        }
        for (role, phase, schema), body in bodies.items():
            with self.subTest(role=role):
                hostile = HostileRuntime([emit(body)], phases=(phase,), num_turns=None)
                with register_runtime("fake", hostile):
                    result = pipe.invoke(role, phase, "emit", schema, runtime_name="fake")
                self.assertTrue(result.success)
                self.assertEqual(len([c for c in hostile.calls if c["phase"] == phase]), 1)

        consult_rt = HostileRuntime([emit(ANSWERS_BODY)], num_turns=None)
        with register_runtime("fake", consult_rt):
            answers = pipe.consult("architect", ["q"], from_role="implementer")
        self.assertTrue(answers)
        self.assertEqual(len([c for c in consult_rt.calls if c["phase"].startswith("consult")]), 1)

        for role, phase in (("test-writer", "test-writer-gate"), ("implementer", "implementer-gate")):
            gate = HostileRuntime([emit(GATE_READY)], phases=(phase,), num_turns=None)
            with register_runtime("fake", gate):
                result = pipe.invoke(
                    role, phase, "gate", "gate.json", capability="read-only", runtime_name="fake"
                )
            self.assertTrue(result.success)
            self.assertEqual(len([c for c in gate.calls if c["phase"] == phase]), 1)


class GuardianSchemaUnjudgedTests(unittest.TestCase):
    def test_guardian_schema_accepts_null_ok(self):
        schema = load_json(schema_path("guardian.json"))
        payload = {
            "risks": [],
            "guardian_markdown": "unjudged cell",
            "chain": {
                "r_to_a": {"ok": True, "note": "n"},
                "a_to_t": {"ok": True, "note": "n"},
                "t_to_i": {"ok": None, "note": "not yet judged"},
                "i_to_r": {"ok": False, "note": "n"},
            },
        }
        errors = validate_schema(payload, schema, enums=False)
        self.assertEqual(errors, [], errors)


class DiscoverCallersCensusTests(unittest.TestCase):
    def test_pipeline_host_suite_passes_fence_test_root(self):
        src = (ROOT / "src" / "team").rglob("*.py")
        missing = []
        for path in src:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = ""
                if isinstance(func, ast.Attribute) and func.attr == "discover_test_command":
                    name = "discover_test_command"
                elif isinstance(func, ast.Name) and func.id == "discover_test_command":
                    name = "discover_test_command"
                if name != "discover_test_command":
                    continue
                dump = ast.dump(node)
                if "test_root" not in dump:
                    missing.append("%s:%s" % (path.relative_to(ROOT).as_posix(), node.lineno))
        self.assertEqual(
            missing,
            [],
            "every discover_test_command call must supply cfg.test_root: %s" % missing,
        )


class InspectWriteAuthorityTests(unittest.TestCase):
    def test_engineering_names_who_writes_the_tree(self):
        text = (ROOT / "docs" / "engineering.md").read_text(encoding="utf-8")
        self.assertIn("## Who writes the tree", text)
        self.assertIn("implementer", text)
        self.assertIn("test-writer", text)
        self.assertIn("team apply", text)
        self.assertIn("reviewer", text)

    def test_reviewer_persona_forbids_repo_docs(self):
        text = (ROOT / "personas" / "reviewer.md").read_text(encoding="utf-8")
        self.assertIn("## Do not write", text)
        self.assertIn("AGENTS.md", text)
        self.assertIn("README", text)

    def test_inspect_only_lines_name_docs_and_apply(self):
        from team.pipeline import start_feature

        repo = Path(tempfile.mkdtemp())
        init_repo(repo)
        cfg = load_config(repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_feature(cfg, "brief", "inspect-only")
        blob = "\n".join(pipe._inspect_only_lines())
        self.assertIn("INSPECT ONLY", blob)
        self.assertIn("AGENTS.md", blob)
        self.assertIn("team apply", blob)


if __name__ == "__main__":
    unittest.main()
