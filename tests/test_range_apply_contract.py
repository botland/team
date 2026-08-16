"""Range-apply contract: seams the kind=test finding said existing suites skip.

Restore, session, unwrap, ignored membership, I→R, collect fail-closed, and
host routing must evaluate the named property — not exception text, headings,
resume=True, or the opposite Grok envelope.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.cli import main
from team.config import load_config
from team.findings import (
    FindingsError,
    collect_guardian_findings,
    collect_review_findings,
    empty_seq_state,
    pick_next_seq,
)
from team.gitutil import changed_paths, porcelain_paths, revert_product, snapshot
from team.pipeline import PipelineError, start_feature, start_range_review
from team.runners import FakeRuntime
from team.state import State
from team.util import dump_json
from tests.support.hostile import (
    HostileRuntime,
    emit,
    register_runtime,
    write,
)
from tests.support.repo import git, head_sha, init_repo
from tests.support.verify_report import VIOLATIONS_HEADING, heading_paths


def _declared_test_names(*texts: str) -> set[str]:
    """Exact `def test_…(` identifiers. Substring search is not membership."""
    names: set[str] = set()
    for text in texts:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("def test_"):
                names.add(stripped[4:].split("(")[0])
    return names


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
GATE_READY = {"ready": True, "consult": "none", "questions": []}
WRITE_SUMMARY = {"summary": "hostile", "paths_touched": ["src/keep.py"]}
REVIEW_OK = {"summary": "ok", "findings": [], "review_markdown": "ok"}
ANSWERS = {"answers_markdown": "proceed"}
CRITIC_REJECT = {
    "accepts": False,
    "issues": ["brief uncovered"],
    "attacks": [],
    "critic_markdown": "reject",
}
GUARDIAN_OK = {
    "risks": [],
    "chain": {
        "r_to_a": {"ok": True, "note": "n"},
        "a_to_t": {"ok": True, "note": "n"},
        "t_to_i": {"ok": True, "note": "n"},
        "i_to_r": {"ok": True, "note": "n"},
    },
    "guardian_markdown": "ok",
}


def _pin_review(work: Path, payload: dict, name="reviewer-fake.result.json") -> None:
    path = work / "prompts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(path, payload)
    state = State.load(work)
    rec = dict(state.last_review or {})
    rec["attempt"] = rec.get("attempt") or 1
    rec["results"] = [
        {"name": path.name, "digest": hashlib.sha256(path.read_bytes()).hexdigest()}
    ]
    state.last_review = rec
    state.save(work)


def _write_guardian(work: Path, payload: dict) -> None:
    body = dict(payload)
    body.setdefault("num_turns", 2)
    body.setdefault(
        "_meta", {"role": "guardian", "phase": "guardian", "num_turns": 2}
    )
    dump_json(work / "prompts" / "guardian.result.json", body)


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

    def _cfg(self, **kw):
        kw.setdefault("fake", True)
        kw.setdefault("force", True)
        kw.setdefault("code_root", "src")
        kw.setdefault("test_root", "tests")
        return load_config(self.repo, **kw)

    def _pipe(self, slug="contract", **cfg_kw):
        return start_feature(self._cfg(**cfg_kw), "contract brief", slug)

    def _commit_ignore(self, rule="secret.env\n*.ignored\n"):
        (self.repo / ".gitignore").write_text(rule, encoding="utf-8")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-m", "ignore")


class IgnoredFenceTests(SeededRepoTests):
    def test_inspect_ignored_product_write_is_violation_and_restored(self):
        self._commit_ignore()
        pipe = self._pipe(slug="ign-inspect")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [write("secret.env", "ignored-pwned\n"), emit(DESIGN_BODY)],
            phases=("future-readonly",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.invoke(
                    "architect",
                    "future-readonly",
                    "hostile",
                    "design.json",
                    capability="read-only",
                )
        self.assertIn("secret.env", str(ctx.exception))
        verify = pipe.work / "git" / "verify-future-readonly.md"
        self.assertTrue(verify.is_file(), "inspect hop must write a verify report")
        self.assertIn(
            "secret.env",
            heading_paths(verify.read_text(encoding="utf-8"), VIOLATIONS_HEADING),
        )
        self.assertFalse((self.repo / "secret.env").exists())
        self.assertEqual(head_sha(self.repo), head_before)

    def test_inspect_already_dirty_ignored_wip_restores_wip_bytes(self):
        self._commit_ignore()
        target = self.repo / "secret.env"
        target.write_text("user-ignored-wip\n", encoding="utf-8")
        pipe = self._pipe(slug="ign-wip")
        hostile = HostileRuntime(
            [write("secret.env", "pwned-ignored\n"), emit(DESIGN_BODY)],
            phases=("future-readonly",),
            num_turns=2,
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
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "user-ignored-wip\n")

    def test_write_in_role_ignored_path_persists(self):
        self._commit_ignore("src/*.ignored\n")
        pipe = self._pipe(slug="ign-inrole")
        rel = "src/keep.ignored"
        hostile = HostileRuntime(
            [
                write(rel, "in-role-ignored\n"),
                emit({"summary": "ok", "paths_touched": [rel]}),
            ],
            phases=("implementer",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            pipe.phase_implementer()
        self.assertEqual((self.repo / rel).read_text(encoding="utf-8"), "in-role-ignored\n")
        verify = (pipe.work / "git" / "verify-implementer.md").read_text(encoding="utf-8")
        self.assertNotIn(rel, heading_paths(verify, VIOLATIONS_HEADING))

    def test_write_out_of_root_ignored_path_is_violation_and_restored(self):
        self._commit_ignore("tests/secret.env\n")
        pipe = self._pipe(slug="ign-oor")
        head_before = head_sha(self.repo)
        hostile = HostileRuntime(
            [
                write("tests/secret.env", "stolen-ignored\n"),
                emit({"summary": "ok", "paths_touched": ["tests/secret.env"]}),
            ],
            phases=("implementer",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_implementer()
        self.assertIn("tests/secret.env", str(ctx.exception))
        self.assertIn("outside", str(ctx.exception).lower())
        self.assertFalse((self.repo / "tests" / "secret.env").exists())
        self.assertEqual(head_sha(self.repo), head_before)

    def test_membership_functions_agree_on_ignored_path(self):
        self._commit_ignore()
        before = snapshot(self.repo)
        (self.repo / "secret.env").write_text("ignored-bytes\n", encoding="utf-8")
        after = snapshot(self.repo)
        delta = changed_paths(self.repo, before, after)
        self.assertIn("secret.env", delta)
        revert_product(self.repo, before)
        self.assertFalse((self.repo / "secret.env").exists())


class SessionIdentityTests(SeededRepoTests):
    def test_invoke_always_mints_session_and_passes_resume_false(self):
        pipe = self._pipe(slug="mint")
        pipe.state.sessions["architect:fake"] = "stored-prior-id"
        pipe.save()
        hostile = HostileRuntime([emit(DESIGN_BODY)], phases=("architect",), num_turns=2)
        with register_runtime("fake", hostile):
            pipe.invoke("architect", "architect", "design", "design.json")
        self.assertEqual(len(hostile.calls), 1)
        call = hostile.calls[0]
        self.assertTrue(call["session_id"])
        self.assertIs(call["resume"], False)
        self.assertNotEqual(call["session_id"], "stored-prior-id")

    def test_hop_space_does_not_resume_a_stored_id(self):
        calls = []
        pipe = self._pipe(slug="hop-space")
        pipe.state.sessions["architect:fake"] = "stored-architect"
        pipe.state.sessions["test-writer:fake"] = "stored-tw"
        pipe.state.sessions["implementer:fake"] = "stored-impl"
        pipe.save()

        class RetryThenWrite(HostileRuntime):
            def complete(self, **kwargs):
                calls.append(
                    {
                        "phase": kwargs.get("phase"),
                        "session_id": kwargs.get("session_id"),
                        "resume": kwargs.get("resume"),
                    }
                )
                return super().complete(**kwargs)

        by_phase = {
            "consult-001": [emit(ANSWERS)],
            "test-writer-gate": [emit(GATE_READY)],
            "test-writer": [
                write("tests/test_ok.py", "def test_ok():\n    assert True\n"),
                emit({"summary": "ok", "paths_touched": ["tests/test_ok.py"]}),
            ],
            "critic": [emit(CRITIC_REJECT)],
            "architect-revise": [emit(DESIGN_BODY)],
            "implementer-apply": [
                write("src/keep.py", "patched\n"),
                emit(WRITE_SUMMARY),
            ],
            "seq-reviewer-fake": [emit(REVIEW_OK)],
        }
        hostile = RetryThenWrite(by_phase=by_phase, num_turns=2)
        reviewer_turns = {"n": 0}

        orig = hostile.complete

        def with_retry(**kwargs):
            phase = kwargs.get("phase")
            if phase == "reviewer-fake" and reviewer_turns["n"] == 0:
                reviewer_turns["n"] += 1
                hostile.num_turns = 1
            else:
                hostile.num_turns = 2
            return orig(**kwargs)

        hostile.complete = with_retry
        seq_dir = pipe.work / "seq" / "deadbeefcafe"
        (seq_dir / "prompts").mkdir(parents=True)
        with register_runtime("fake", hostile):
            pipe.consult("architect", ["q"], from_role="tdd-design")
            pipe.phase_test_writer()
            pipe.phase_critic()
            pipe.invoke("reviewer", "reviewer-fake", "inspect first", "review.json")
            pipe._apply_implementer(
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
            )
            pipe.phase_seq_review(seq_dir, [{"kind": "test", "title": "x"}])
        self.assertTrue(calls, "hop space must invoke complete")
        self.assertTrue(all(c["resume"] is False for c in calls), calls)
        sids = [c["session_id"] for c in calls]
        self.assertTrue(all(sids), sids)
        self.assertEqual(len(sids), len(set(sids)), sids)
        self.assertTrue(all(sid not in ("stored-architect", "stored-tw", "stored-impl") for sid in sids))
        retry = [c for c in calls if c["phase"] == "reviewer-fake"]
        self.assertGreaterEqual(len(retry), 2, calls)
        self.assertEqual(len({c["session_id"] for c in retry}), len(retry))

    def test_feature_pipeline_every_hop_is_a_fresh_session(self):
        calls = []
        orig = FakeRuntime.complete

        def spy(self, **kwargs):
            calls.append(
                {
                    "phase": kwargs.get("phase"),
                    "session_id": kwargs.get("session_id"),
                    "resume": kwargs.get("resume"),
                }
            )
            return orig(self, **kwargs)

        with mock.patch.object(FakeRuntime, "complete", spy):
            rc = main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--test-command",
                    "true",
                    "feature",
                    "--force",
                    "Add greet helper",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertGreater(len(calls), 1, calls)
        self.assertTrue(all(c["resume"] is False for c in calls), calls)
        sids = [c["session_id"] for c in calls]
        self.assertTrue(all(sids), sids)
        self.assertEqual(len(sids), len(set(sids)), sids)


class CollectApplyAuthorityTests(SeededRepoTests):
    def test_collect_and_apply_fail_closed_on_unreadable_or_non_object_reviewer_result(self):
        pipe = self._pipe(slug="trunc-review")
        prompts = pipe.work / "prompts"
        prompts.mkdir(exist_ok=True)
        pin = prompts / "reviewer-fake.result.json"
        cases = (
            '{"summary": "trunc", "findings": [',
            '["not", "an", "object"]',
        )
        for body in cases:
            with self.subTest(body=body[:20]):
                pin.write_text(body, encoding="utf-8")
                digest = hashlib.sha256(pin.read_bytes()).hexdigest()
                state = State.load(pipe.work)
                state.last_review = {
                    "attempt": 1,
                    "results": [{"name": pin.name, "digest": digest}],
                }
                state.save(pipe.work)
                with self.assertRaises(FindingsError):
                    collect_review_findings(pipe.work)
        pin.unlink(missing_ok=True)
        state = State.load(pipe.work)
        state.last_review = {
            "attempt": 1,
            "results": [{"name": "reviewer-fake.result.json", "digest": "dead"}],
        }
        state.save(pipe.work)
        with self.assertRaises(FindingsError):
            collect_review_findings(pipe.work)

        pipe.write_artifact("review.md", "# Review\nfindings exist\n")
        pin.write_text('{"summary": "trunc", "findings": [', encoding="utf-8")
        digest = hashlib.sha256(pin.read_bytes()).hexdigest()
        state = State.load(pipe.work)
        state.last_review = {
            "attempt": 1,
            "results": [{"name": pin.name, "digest": digest}],
        }
        state.save(pipe.work)
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "trunc-review",
            ]
        )
        self.assertNotEqual(rc, 0)
        self.assertNotEqual(State.load(pipe.work).stop_reason, "applied")
        self.assertFalse((pipe.work / "apply-impl-summary.md").is_file())

    def test_dual_review_truncated_file_does_not_apply_the_other_side(self):
        pipe = self._pipe(slug="dual-trunc")
        prompts = pipe.work / "prompts"
        prompts.mkdir(exist_ok=True)
        complete = {
            "summary": "A",
            "findings": [
                {
                    "severity": "high",
                    "title": "finding A",
                    "evidence": "e",
                    "path": "src/keep.py",
                    "kind": "implementation",
                }
            ],
        }
        dump_json(prompts / "reviewer-claude.result.json", complete)
        (prompts / "reviewer-grok.result.json").write_text(
            '{"summary": "trunc", "findings": [', encoding="utf-8"
        )
        pipe.write_artifact("review.md", "# Review\n## claude\n## grok\n")
        state = State.load(pipe.work)
        state.last_review = {
            "attempt": 1,
            "results": [
                {
                    "name": "reviewer-claude.result.json",
                    "digest": hashlib.sha256(
                        (prompts / "reviewer-claude.result.json").read_bytes()
                    ).hexdigest(),
                },
                {
                    "name": "reviewer-grok.result.json",
                    "digest": hashlib.sha256(
                        (prompts / "reviewer-grok.result.json").read_bytes()
                    ).hexdigest(),
                },
            ],
        }
        state.save(pipe.work)
        with self.assertRaises((FindingsError, Exception)):
            collect_review_findings(pipe.work)
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "dual-trunc",
            ]
        )
        self.assertNotEqual(rc, 0)
        self.assertNotEqual(State.load(pipe.work).stop_reason, "applied")

    def test_unreadable_guardian_result_is_fail_closed(self):
        pipe = self._pipe(slug="bad-guardian")
        pipe.write_artifact("review.md", "# Review\n")
        _pin_review(
            pipe.work,
            {
                "summary": "ok",
                "findings": [
                    {
                        "severity": "low",
                        "title": "note only",
                        "evidence": "e",
                        "path": "README",
                        "kind": "note",
                    }
                ],
            },
        )
        (pipe.work / "prompts" / "guardian.result.json").write_text(
            '{"risks": [', encoding="utf-8"
        )
        try:
            rows = collect_guardian_findings(pipe.work)
        except (FindingsError, Exception):
            rows = None
        self.assertIsNone(
            rows,
            "unreadable guardian.result.json must fail closed, not return %r" % rows,
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "bad-guardian",
            ]
        )
        self.assertNotEqual(rc, 0)
        self.assertNotEqual(State.load(pipe.work).stop_reason, "applied")

    def test_apply_unstructured_findings_stop_without_a_reviewer_hop(self):
        pipe = self._pipe(slug="needs-kind")
        pipe.write_artifact("review.md", "# Review\nunclassified\n")
        _pin_review(
            pipe.work,
            {
                "summary": "ok",
                "findings": [
                    {
                        "severity": "high",
                        "title": "no kind",
                        "evidence": "e",
                        "path": "src/keep.py",
                    }
                ],
            },
        )
        calls = []
        orig = FakeRuntime.complete

        def spy(self, **kwargs):
            calls.append(kwargs.get("phase"))
            return orig(self, **kwargs)

        with mock.patch.object(FakeRuntime, "complete", spy):
            pipe.apply_review()
        self.assertEqual(pipe.state.stop_reason, "needs-classification")
        self.assertFalse(any(str(p).startswith("reviewer") for p in calls), calls)
        self.assertFalse(any(str(p).startswith("guardian") for p in calls), calls)


class GuardianApplyRailTests(SeededRepoTests):
    def test_guardian_i_to_r_explicit_risk_is_implementation(self):
        pipe = self._pipe(slug="i2r-explicit")
        _write_guardian(
            pipe.work,
            {
                "risks": [
                    {
                        "title": "brief not shipped",
                        "evidence": "no handler",
                        "path": "src/keep.py",
                        "link": "i_to_r",
                    },
                    {
                        "title": "contract hole",
                        "evidence": "no reject",
                        "path": "tests/test_a.py",
                        "link": "a_to_t",
                    },
                ],
                "guardian_markdown": "ok",
                "chain": GUARDIAN_OK["chain"],
            },
        )
        rows = collect_guardian_findings(pipe.work)
        by_link = {}
        for row in rows:
            for link in ("i_to_r", "a_to_t"):
                if link in row["title"]:
                    by_link[link] = row
        self.assertEqual(by_link["i_to_r"]["kind"], "implementation")
        self.assertEqual(by_link["a_to_t"]["kind"], "test")

    def test_guardian_i_to_r_synthetic_cell_is_implementation(self):
        pipe = self._pipe(slug="i2r-synth")
        _write_guardian(
            pipe.work,
            {
                "risks": [],
                "guardian_markdown": "ok",
                "chain": {
                    "r_to_a": {"ok": True, "note": "n"},
                    "a_to_t": {"ok": True, "note": "n"},
                    "t_to_i": {"ok": True, "note": "n"},
                    "i_to_r": {"ok": False, "note": "missed brief"},
                },
            },
        )
        rows = collect_guardian_findings(pipe.work)
        synth = [r for r in rows if "i_to_r" in r["title"]]
        self.assertEqual(len(synth), 1, rows)
        self.assertEqual(synth[0]["kind"], "implementation")
        self.assertEqual(synth[0]["source"], "guardian")
        self.assertEqual(synth[0]["severity"], "invariant")
        self.assertIn("[i_to_r] failed chain cell", synth[0]["title"])

        _write_guardian(
            pipe.work,
            {
                "risks": [
                    {
                        "title": "tree missed the brief",
                        "evidence": "invariant",
                        "path": "src/keep.py",
                        "link": "invariant",
                    }
                ],
                "guardian_markdown": "ok",
                "chain": {
                    "r_to_a": {"ok": True, "note": "n"},
                    "a_to_t": {"ok": True, "note": "n"},
                    "t_to_i": {"ok": True, "note": "n"},
                    "i_to_r": {"ok": False, "note": "still open"},
                },
            },
        )
        rows = collect_guardian_findings(pipe.work)
        self.assertTrue(
            any(r["kind"] == "architecture" and "invariant" in r["title"] for r in rows)
        )
        self.assertTrue(
            any("i_to_r" in r["title"] and r["kind"] == "implementation" for r in rows)
        )

    def test_guardian_closed_link_map_and_unknown_unclassified(self):
        from team.findings import needs_classify, seq_candidates, finding_id

        pipe = self._pipe(slug="link-map")
        risks = [
            {"title": "r", "evidence": "r_to_a", "path": "a", "link": "r_to_a"},
            {"title": "a", "evidence": "a_to_t", "path": "a", "link": "a_to_t"},
            {"title": "t", "evidence": "t_to_i", "path": "a", "link": "t_to_i"},
            {"title": "i", "evidence": "i_to_r", "path": "a", "link": "i_to_r"},
            {"title": "inv", "evidence": "invariant", "path": "a", "link": "invariant"},
            {"title": "empty", "evidence": "empty", "path": "a", "link": ""},
            {"title": "omit", "evidence": "omit", "path": "a"},
            {"title": "typo", "evidence": "t2i", "path": "a", "link": "t2i"},
            {"title": "case", "evidence": "T_TO_I", "path": "a", "link": "T_TO_I"},
            {
                "title": "spoof",
                "evidence": "spoof",
                "path": "a",
                "link": "t_to_i",
                "kind": "architecture",
            },
            {"title": "mystery", "evidence": "mystery", "path": "a", "link": "mystery"},
        ]
        _write_guardian(
            pipe.work,
            {"risks": risks, "guardian_markdown": "ok", "chain": GUARDIAN_OK["chain"]},
        )
        rows = collect_guardian_findings(pipe.work)
        by_ev = {r["evidence"]: r for r in rows}
        expect = {
            "r_to_a": "architecture",
            "a_to_t": "test",
            "t_to_i": "implementation",
            "i_to_r": "implementation",
            "invariant": "architecture",
            "empty": "unclassified",
            "omit": "unclassified",
            "t2i": "unclassified",
            "T_TO_I": "implementation",
            "spoof": "implementation",
            "mystery": "unclassified",
        }
        for ev, kind in expect.items():
            self.assertEqual(by_ev[ev]["kind"], kind, ev)
        self.assertTrue(needs_classify(rows))
        cands = {finding_id(r) for r in seq_candidates(rows, empty_seq_state())}
        for row in rows:
            if row["kind"] == "unclassified":
                self.assertNotIn(finding_id(row), cands)

    def test_apply_i_to_r_does_not_enter_replan(self):
        pipe = self._pipe(slug="i2r-apply")
        pipe.cfg.test_command = "true"
        pipe.state.test_command = "true"
        pipe.write_artifact("review.md", "# Review\nnotes only\n")
        _pin_review(
            pipe.work,
            {
                "summary": "notes",
                "findings": [
                    {
                        "severity": "low",
                        "title": "residual note",
                        "evidence": "e",
                        "path": "README",
                        "kind": "note",
                    }
                ],
            },
        )
        _write_guardian(
            pipe.work,
            {
                "risks": [
                    {
                        "title": "brief not shipped",
                        "evidence": "no handler",
                        "path": "src/keep.py",
                        "link": "i_to_r",
                    }
                ],
                "guardian_markdown": "ok",
                "chain": GUARDIAN_OK["chain"],
            },
        )
        rows = collect_guardian_findings(pipe.work)
        nxt = pick_next_seq(rows, empty_seq_state())
        self.assertEqual(nxt["kind"], "implementation")
        self.assertNotEqual(nxt["kind"], "architecture")
        hostile = HostileRuntime(
            [
                write("src/keep.py", "patched\n"),
                emit(WRITE_SUMMARY),
            ],
            phases=("implementer-apply",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            pipe.apply_review()
        self.assertFalse((pipe.work / "design-replan.md").is_file())
        plan = (pipe.work / "apply-plan.md").read_text(encoding="utf-8")
        self.assertIn("implementation", plan.lower())
        self.assertTrue(
            (pipe.work / "apply-impl-summary.md").is_file()
            or "implementer" in "\n".join(pipe.log_lines),
        )
        self.assertNotEqual(pipe.state.stop_reason, "needs-classification")
        self.assertFalse(
            any((c.get("phase") or "") in ("replan", "replan-questions") for c in hostile.calls)
        )
        self.assertNotIn("architect replan", "\n".join(pipe.log_lines))

    def test_apply_invariant_and_r_to_a_still_replan(self):
        pipe = self._pipe(slug="inv-replan")
        pipe.cfg.test_command = "true"
        pipe.state.test_command = "true"
        pipe.write_artifact("review.md", "# Review\n")
        pipe.write_artifact("design.md", "# Design\n")
        _pin_review(
            pipe.work,
            {
                "summary": "notes",
                "findings": [
                    {
                        "severity": "low",
                        "title": "residual note",
                        "evidence": "e",
                        "path": "README",
                        "kind": "note",
                    }
                ],
            },
        )
        _write_guardian(
            pipe.work,
            {
                "risks": [
                    {
                        "title": "tree missed the brief",
                        "evidence": "invariant",
                        "path": "src/keep.py",
                        "link": "invariant",
                    }
                ],
                "guardian_markdown": "ok",
                "chain": GUARDIAN_OK["chain"],
            },
        )
        rows = collect_guardian_findings(pipe.work)
        self.assertTrue(all(r["kind"] == "architecture" for r in rows), rows)
        pipe.apply_review()
        self.assertTrue((pipe.work / "design-replan.md").is_file())

    def test_range_apply_architecture_replans_without_prior_design(self):
        """Architecture findings run replan. Missing design.md is the reason to write A."""
        pipe = start_range_review(self._cfg(), slug="range-arch")
        pipe.cfg.test_command = "true"
        pipe.state.test_command = "true"
        pipe.write_artifact("review.md", "# Review\n")
        self.assertFalse(pipe.read_artifact("design.md").strip())
        _pin_review(
            pipe.work,
            {
                "summary": "fence",
                "findings": [
                    {
                        "severity": "high",
                        "title": "fence belongs in invoke",
                        "evidence": "e",
                        "path": "src/keep.py",
                        "kind": "architecture",
                    }
                ],
            },
        )
        pipe.apply_review()
        self.assertTrue((pipe.work / "design-replan.md").is_file())
        self.assertTrue(pipe.read_artifact("design.md").strip())
        log = "\n".join(pipe.log_lines)
        self.assertIn("architect replan", log)
        self.assertNotIn("architecture rides implementation", log)

    def test_range_apply_test_writes_contract_when_missing(self):
        pipe = start_range_review(self._cfg(), slug="range-test")
        pipe.cfg.test_command = "true"
        pipe.state.test_command = "true"
        pipe.write_artifact("review.md", "# Review\n")
        self.assertFalse(pipe.read_artifact("test-contract.md").strip())
        _pin_review(
            pipe.work,
            {
                "summary": "vacuous tests",
                "findings": [
                    {
                        "severity": "medium",
                        "title": "fence tests can pass without the property",
                        "evidence": "e",
                        "path": "tests/test_a.py",
                        "kind": "test",
                    }
                ],
            },
        )
        pipe.apply_review()
        log = "\n".join(pipe.log_lines)
        self.assertIn("apply: tdd-design", log)
        self.assertTrue(
            (pipe.work / "prompts" / "tdd-design-apply.result.json").is_file()
        )
        self.assertTrue(pipe.read_artifact("test-contract.md").strip())
        self.assertIn("test-writer", log)

    def test_findings_tests_do_not_pin_i_to_r_as_architecture(self):
        text = (ROOT / "tests" / "test_findings.py").read_text(encoding="utf-8")
        self.assertIn('"i_to_r": "implementation"', text)
        self.assertNotIn('"i_to_r": "architecture"', text)
        self.assertNotIn("i_to_r stays architecture", text)
        self.assertNotRegex(
            text,
            r'i_to_r["\'].*kind["\'].*architecture|kind == "architecture".*i_to_r',
        )

    def test_seq_unverified_is_not_class_failure(self):
        pipe = self._pipe(slug="seq-unv", test_command="")
        pipe.cfg.test_command = ""
        pipe.state.test_command = ""
        pipe.write_artifact("review.md", "# Review\n")
        _pin_review(
            pipe.work,
            {
                "summary": "ok",
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
        hostile = HostileRuntime(
            [write("src/keep.py", "patched\n"), emit(WRITE_SUMMARY)],
            phases=("implementer-apply",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            pipe.apply_review(seq=True)
        self.assertNotEqual(pipe.state.stop_reason, "seq-failed")

    def test_seq_collection_death_is_not_class_failure(self):
        cmd = (
            "python3 -c \"print('ERROR collecting tests/unit/test_x.py\\n"
            "Interrupted: 1 errors during collection'); raise SystemExit(2)\""
        )
        pipe = self._pipe(slug="seq-collect", test_command=cmd)
        pipe.cfg.test_command = cmd
        pipe.state.test_command = cmd
        pipe.write_artifact("review.md", "# Review\n")
        _pin_review(
            pipe.work,
            {
                "summary": "ok",
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
        hostile = HostileRuntime(
            [write("src/keep.py", "patched\n"), emit(WRITE_SUMMARY)],
            phases=("implementer-apply",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            pipe.apply_review(seq=True)
        self.assertNotEqual(pipe.state.stop_reason, "seq-failed")
        self.assertNotIn("failed", (pipe.state.stop_reason or ""))


class ContractMutationCheckTests(unittest.TestCase):
    def test_write_fence_restore_cases_mutation_check_pre_hop_blobs(self):
        restore_files = (
            ROOT / "tests" / "test_write_fence.py",
            ROOT / "tests" / "test_architecture_delta.py",
            ROOT / "tests" / "test_range_apply_contract.py",
        )
        texts = [p.read_text(encoding="utf-8") for p in restore_files]
        blob = "\n".join(texts)
        self.assertIn("read_text", blob)
        self.assertIn("head_before", blob)
        self.assertIn("pre_bytes", blob)
        self.assertIn("_assert_forbidden_tree_restored", blob)
        names = _declared_test_names(*texts)
        self.assertNotIn("test_already_dirty_edit_is_a_violation", names)

    def test_session_and_unwrap_suites_cannot_pass_on_the_withdrawn_pins(self):
        util = (ROOT / "tests" / "test_util.py").read_text(encoding="utf-8")
        runners = (ROOT / "tests" / "test_runners.py").read_text(encoding="utf-8")
        pipeline = (ROOT / "tests" / "test_pipeline_fake.py").read_text(encoding="utf-8")
        fence = (ROOT / "tests" / "test_write_fence.py").read_text(encoding="utf-8")
        testhost = (ROOT / "tests" / "test_testhost.py").read_text(encoding="utf-8")
        findings = (ROOT / "tests" / "test_findings.py").read_text(encoding="utf-8")
        here = Path(__file__).read_text(encoding="utf-8")
        combined = "\n".join([util, runners, pipeline, fence, testhost, findings, here])
        self.assertIn("test_incomplete_empty_structured_output_does_not_beat_complete_text", combined)
        self.assertIn(
            "test_finished_empty_review_structured_output_beats_stale_text_findings",
            combined,
        )
        self.assertIn('"text": json.dumps(_REVIEW)', util)
        names = _declared_test_names(
            util, runners, pipeline, fence, testhost, findings, here
        )
        self.assertNotIn("test_existing_session_resumes_instead_of_recreate", names)
        self.assertIn("resume is False", combined)
        self.assertIn("secret.env", here)
        self.assertIn("user-ignored-wip", here)
        self.assertIn('"i_to_r": "implementation"', findings)
        self.assertIn('kind"], "implementation"', here)
        self.assertIn("jest: the test suite failed to run", testhost)
        self.assertIn("test_evidence_distinct_rows_stay_two_seq_classes", names)
        self.assertIn("test_path_spellings_are_one_finding_and_one_related_path", names)
        self.assertIn("test_nested_test_root_under_code_root_is_the_same_hop", names)
        self.assertIn(
            "test_write_outside_repo_is_fence_error_and_does_not_persist", names
        )
        self.assertIn("run_suite", testhost)
        self.assertIn("ERROR collecting", testhost)
        audit = (ROOT / "tests" / "test_audit_fake.py").read_text(encoding="utf-8")
        audit_names = _declared_test_names(audit)
        self.assertIn(
            "test_non_git_audit_product_write_is_fence_error_and_does_not_persist",
            audit_names,
        )


if __name__ == "__main__":
    unittest.main()


class ApplySurfaceBudgetTests(SeededRepoTests):
    """The patch a hop is handed is capped; the fence and the path list are not."""

    def _fat_tree(self):
        (self.repo / "coverage-html").mkdir()
        (self.repo / "coverage-html" / "main_py.html").write_text(
            "<p>generated</p>\n" * 20000, encoding="utf-8"
        )
        (self.repo / "src" / "keep.py").write_text(KEEP + "# edited\n", encoding="utf-8")

    def test_generated_bulk_is_omitted_but_still_named(self):
        self._fat_tree()
        pipe = self._pipe(slug="surface-budget")
        pipe.cfg.diff_budget = 64 * 1024
        pipe._write_apply_surface()
        patch = (pipe.work / "git" / "apply.patch").read_text(encoding="utf-8")
        names = (pipe.work / "git" / "apply-names.txt").read_text(encoding="utf-8")
        self.assertNotIn("<p>generated</p>", patch, "the bulk must not be in the patch")
        self.assertIn("omitted from this patch", patch)
        self.assertIn("coverage-html/main_py.html", patch, "the note names it")
        self.assertIn(
            "coverage-html/main_py.html", names, "the path list stays complete"
        )
        self.assertIn("src/keep.py", patch, "real work still gets reviewed")

    def test_budget_does_not_shrink_the_write_fence(self):
        self._fat_tree()
        pipe = self._pipe(slug="surface-fence")
        pipe.cfg.diff_budget = 64 * 1024
        pipe._write_apply_surface()
        dirty = porcelain_paths(self.repo)
        self.assertIn("coverage-html/main_py.html", dirty)
        self.assertIn("src/keep.py", dirty)

    def test_no_budget_keeps_every_byte(self):
        self._fat_tree()
        pipe = self._pipe(slug="surface-nocap")
        pipe.cfg.diff_budget = 0
        pipe._write_apply_surface()
        patch = (pipe.work / "git" / "apply.patch").read_text(encoding="utf-8")
        self.assertIn("<p>generated</p>", patch)
        self.assertNotIn("omitted from this patch", patch)

    def test_a_clean_tree_gets_no_omission_header(self):
        pipe = self._pipe(slug="surface-clean")
        pipe._write_apply_surface()
        patch = (pipe.work / "git" / "apply.patch").read_text(encoding="utf-8")
        self.assertNotIn("omitted", patch)
