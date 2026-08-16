"""Finding kinds, collection scope, and merge overlap.

Covers unknown-kind silent notes, stale reviewer globs, and overlap misreport.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.cli import main
from team.config import load_config, schema_path
from team.findings import (
    KINDS,
    collect_review_findings,
    fill_missing_kinds,
    group_by_kind,
    needs_classify,
    normalize_kind,
    render_plan,
)
from tests.support.hostile import HostileRuntime, emit, register_runtime
from team.merge import merge_reviews
from team.pipeline import start_audit, start_feature, start_range_review
from team.state import State
from team.util import dump_json, load_json
from tests.support.repo import init_repo


def _finding(title, *, kind="", path="src/a.py", evidence="e", severity="high"):
    return {
        "severity": severity,
        "title": title,
        "evidence": evidence,
        "path": path,
        "kind": kind,
    }


def _schema_validate():
    for mod_name, attr in (
        ("team.schemas", "validate"),
        ("team.schema", "validate"),
        ("team.util", "validate_schema"),
    ):
        try:
            mod = __import__(mod_name, fromlist=[attr])
        except ImportError:
            continue
        fn = getattr(mod, attr, None)
        if callable(fn):
            return fn
    return None


class KindBoundaryTests(unittest.TestCase):
    def test_normalize_kind_is_total(self):
        self.assertEqual(normalize_kind("security"), "unclassified")
        self.assertEqual(normalize_kind("correctness"), "unclassified")
        self.assertEqual(normalize_kind(""), "unclassified")
        self.assertEqual(normalize_kind(None), "unclassified")
        self.assertEqual(normalize_kind("Architect"), "architecture")
        self.assertEqual(normalize_kind("IMPL"), "implementation")
        self.assertNotEqual(normalize_kind("mystery"), "")
        self.assertNotEqual(normalize_kind("mystery"), "note")

    def test_unknown_kind_does_not_become_a_note(self):
        filled = fill_missing_kinds([_finding("sec", kind="security")])
        self.assertEqual(filled[0]["kind"], "unclassified")

    def test_needs_classify_on_unclassified_and_unknown(self):
        self.assertTrue(needs_classify([_finding("s", kind="security")]))
        self.assertTrue(needs_classify([_finding("u", kind="unclassified")]))
        self.assertFalse(needs_classify([_finding("t", kind="test")]))

    def test_kind_census_preserves_every_finding(self):
        rows = [
            _finding("s", kind="security"),
            _finding("a", kind="architecture"),
            _finding("e", kind=""),
            _finding("i", kind="implementation"),
            _finding("t", kind="test"),
            _finding("n", kind="note"),
            _finding("A2", kind="Architect"),
        ]
        groups = group_by_kind(rows)
        self.assertEqual(sum(len(v) for v in groups.values()), len(rows))
        allowed = set(KINDS) | {"unclassified"}
        extra = set(groups) - allowed
        self.assertFalse(extra, extra)
        self.assertIn("unclassified", groups)
        unclassified_titles = {item["title"] for item in groups["unclassified"]}
        self.assertIn("s", unclassified_titles)
        self.assertIn("e", unclassified_titles)

    def test_review_schema_enums_kind(self):
        schema = load_json(schema_path("review.json"))
        kind_schema = schema["properties"]["findings"]["items"]["properties"]["kind"]
        self.assertIn("enum", kind_schema)
        enum = set(kind_schema["enum"])
        for required in ("architecture", "implementation", "test", "note"):
            self.assertIn(required, enum)
        self.assertNotIn("security", enum)

    def test_schema_validate_rejects_unknown_kind(self):
        validate = _schema_validate()
        self.assertIsNotNone(validate, "schemas.validate must reject kind=security")
        schema = load_json(schema_path("review.json"))
        payload = {
            "summary": "x",
            "findings": [
                {
                    "severity": "high",
                    "title": "t",
                    "evidence": "e",
                    "kind": "security",
                }
            ],
        }
        errors = validate(payload, schema)
        self.assertTrue(errors)


class ApplyUnclassifiedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def test_render_plan_has_unclassified_section(self):
        rows = fill_missing_kinds([_finding("something wrong", kind="security")])
        plan = render_plan(group_by_kind(rows))
        self.assertIn("unclassified", plan.lower())

    def test_unknown_kind_stops_apply_as_needs_classification(self):
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
        work = self.repo / ".team" / "work" / "add-greet-helper"
        (work / "review.md").write_text("# Review\n", encoding="utf-8")
        injected = {
            "summary": "injected",
            "findings": [_finding("something wrong", kind="security", path="src/greet.py")],
        }
        dump_json(work / "prompts" / "reviewer-fake.result.json", injected)
        hostile = HostileRuntime([emit(injected)], phases=("reviewer-fake",))
        with register_runtime("fake", hostile):
            rc = main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--test-command",
                    "true",
                    "apply",
                    "add-greet-helper",
                ]
            )
        self.assertEqual(rc, 0)
        plan = (work / "apply-plan.md").read_text(encoding="utf-8")
        self.assertIn("unclassified", plan.lower())
        state = State.load(work)
        self.assertEqual(state.stop_reason, "needs-classification")

    def test_invoke_accepts_kind_security_as_unclassified_not_actionable(self):
        from team.config import load_config
        from team.pipeline import PipelineError, start_feature

        cfg = load_config(
            self.repo, fake=True, force=True, code_root="src", test_root="tests"
        )
        pipe = start_feature(cfg, "brief", "kind-security-invoke")
        payload = {
            "summary": "injected",
            "findings": [_finding("something wrong", kind="security", path="src/greet.py")],
            "review_markdown": "progress",
        }
        hostile = HostileRuntime([emit(payload)], phases=("reviewer-fake",))
        with register_runtime("fake", hostile):
            try:
                pipe.phase_reviewer()
            except PipelineError as exc:
                self.fail(
                    "invoke(enums=False) must accept kind=security then classify, got %s"
                    % exc
                )
        found = collect_review_findings(pipe.work)
        self.assertTrue(found)
        self.assertTrue(all(item["kind"] == "unclassified" for item in found), found)
        self.assertTrue(needs_classify(found))
        with register_runtime("fake", hostile):
            rc = main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--test-command",
                    "true",
                    "apply",
                    "kind-security-invoke",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(State.load(pipe.work).stop_reason, "needs-classification")
        self.assertFalse((pipe.work / "apply-impl-summary.md").is_file())

    def test_apply_does_not_classify_premature_progress_as_finished_review(self):
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
        work = self.repo / ".team" / "work" / "add-greet-helper"
        (work / "review.md").write_text("# Review\n", encoding="utf-8")
        dump_json(
            work / "prompts" / "reviewer-fake.result.json",
            {
                "summary": "Reviewing the collected range first.",
                "findings": [
                    _finding(
                        "Review in progress",
                        kind="implementation",
                        path="src/greet.py",
                        evidence="Starting with the orchestrator artifacts.",
                    )
                ],
                "review_markdown": "progress",
            },
        )
        result_path = work / "prompts" / "reviewer-fake.result.json"
        state = State.load(work)
        rec = dict(state.last_review or {})
        rec["results"] = [
            {
                "name": result_path.name,
                "digest": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            }
        ]
        state.last_review = rec
        state.save(work)
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "add-greet-helper",
            ]
        )
        plan = ""
        if (work / "apply-plan.md").is_file():
            plan = (work / "apply-plan.md").read_text(encoding="utf-8")
        self.assertNotIn("Review in progress", plan)
        self.assertFalse((work / "apply-impl-summary.md").is_file())
        self.assertNotEqual(State.load(work).stop_reason, "applied")
        self.assertNotEqual(rc, 0)


class ApplyDigestBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def _feature(self):
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
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "review",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        return self.repo / ".team" / "work" / "add-greet-helper"

    def test_apply_digest_mismatch_is_fail_closed(self):
        work = self._feature()
        dump_json(
            work / "prompts" / "reviewer-fake.result.json",
            {
                "summary": "tampered",
                "findings": [
                    _finding(
                        "greet ignores empty name",
                        kind="implementation",
                        path="src/greet.py",
                    )
                ],
            },
        )
        dump_json(
            work / "prompts" / "reviewer-stale.result.json",
            {
                "summary": "stale extra",
                "findings": [
                    _finding("extra bug", kind="implementation", path="src/greet.py")
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
                "add-greet-helper",
            ]
        )
        self.assertNotEqual(rc, 0)
        self.assertNotEqual(State.load(work).stop_reason, "applied")
        self.assertFalse((work / "apply-impl-summary.md").is_file())

    def test_needs_classify_ignores_unclassified_extra_when_pin_is_classified(self):
        work = self._feature()
        dump_json(
            work / "prompts" / "reviewer-fake.result.json",
            {
                "summary": "pinned",
                "findings": [
                    _finding(
                        "greet ignores empty name",
                        kind="implementation",
                        path="src/greet.py",
                    )
                ],
            },
        )
        dump_json(
            work / "prompts" / "reviewer-stale.result.json",
            {
                "summary": "leftover extra",
                "findings": [_finding("mystery leftover", kind="security", path="src/x.py")],
            },
        )
        pin = work / "prompts" / "reviewer-fake.result.json"
        state = State.load(work)
        rec = dict(state.last_review or {})
        rec["results"] = [
            {"name": pin.name, "digest": hashlib.sha256(pin.read_bytes()).hexdigest()}
        ]
        attempt = int(rec.get("attempt") or 1)
        rec["attempt"] = attempt
        state.last_review = rec
        state.save(work)
        found = collect_review_findings(work)
        self.assertFalse(needs_classify(found, work=work))
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        after = State.load(work)
        self.assertEqual((after.last_review or {}).get("attempt"), attempt)
        plan = (work / "apply-plan.md").read_text(encoding="utf-8")
        self.assertIn("implementation (1)", plan)
        self.assertNotEqual(after.stop_reason, "needs-classification")

    def test_apply_pin_lost_does_not_glob_stale_extras(self):
        work = self._feature()
        recorded = work / "prompts" / "reviewer-fake.result.json"
        self.assertTrue(recorded.is_file())
        recorded.unlink()
        dump_json(
            work / "prompts" / "reviewer-stale.result.json",
            {
                "summary": "stale extra",
                "findings": [
                    _finding("extra bug", kind="implementation", path="src/greet.py")
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
                "add-greet-helper",
            ]
        )
        self.assertNotEqual(rc, 0)
        self.assertNotEqual(State.load(work).stop_reason, "applied")
        self.assertFalse((work / "apply-impl-summary.md").is_file())


class StaleResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        (self.work / "prompts").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_result(self, name, title):
        path = self.work / "prompts" / name
        dump_json(path, {"findings": [_finding(title, kind="implementation")], "summary": title})
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return path, digest

    def _state(self, results, attempt=2):
        dump_json(
            self.work / "state.json",
            {
                "slug": "s",
                "brief": "b",
                "repo": str(self.work),
                "engine_root": str(ROOT),
                "last_review": {"attempt": attempt, "results": results},
            },
        )

    def test_stale_and_current_result(self):
        _stale, _stale_digest = self._write_result("reviewer-claude.result.json", "A")
        _cur, cur_digest = self._write_result("reviewer-grok.result.json", "B")
        self._state(
            [{"name": "reviewer-grok.result.json", "digest": cur_digest}],
            attempt=2,
        )
        found = collect_review_findings(self.work)
        titles = [item["title"] for item in found]
        self.assertEqual(titles, ["B"])

    def test_both_reviewers_one_attempt_keeps_both(self):
        _a, da = self._write_result("reviewer-claude.result.json", "A")
        _b, db = self._write_result("reviewer-grok.result.json", "B")
        self._state(
            [
                {"name": "reviewer-claude.result.json", "digest": da},
                {"name": "reviewer-grok.result.json", "digest": db},
            ],
            attempt=1,
        )
        found = collect_review_findings(self.work)
        titles = {item["title"] for item in found}
        self.assertEqual(titles, {"A", "B"})

    def test_digest_mismatch_is_an_error(self):
        self._write_result("reviewer-grok.result.json", "B")
        self._state(
            [{"name": "reviewer-grok.result.json", "digest": "0" * 64}],
            attempt=2,
        )
        with self.assertRaises(Exception) as ctx:
            collect_review_findings(self.work)
        self.assertIn("digest", str(ctx.exception).lower())

    def test_second_reviewer_copy_is_not_dropped(self):
        dump_json(
            self.work / "prompts" / "reviewer-claude.result.json",
            {
                "findings": [
                    _finding("leak", kind="implementation", evidence="first evidence")
                ],
                "summary": "c",
            },
        )
        dump_json(
            self.work / "prompts" / "reviewer-grok.result.json",
            {
                "findings": [
                    _finding("leak", kind="implementation", evidence="second evidence")
                ],
                "summary": "g",
            },
        )
        da = hashlib.sha256(
            (self.work / "prompts" / "reviewer-claude.result.json").read_bytes()
        ).hexdigest()
        db = hashlib.sha256(
            (self.work / "prompts" / "reviewer-grok.result.json").read_bytes()
        ).hexdigest()
        self._state(
            [
                {"name": "reviewer-claude.result.json", "digest": da},
                {"name": "reviewer-grok.result.json", "digest": db},
            ],
            attempt=1,
        )
        found = collect_review_findings(self.work)
        evidences = {item["evidence"] for item in found}
        self.assertEqual(evidences, {"first evidence", "second evidence"})


def _with_slow_record_as_list(fn):
    """Widen the unlocked last_review RMW window. A lock around the whole
    record step serializes as_list and keeps both pins."""
    import inspect
    import time

    import team.pipeline as pipeline_mod

    orig = pipeline_mod.as_list

    def slow_as_list(value):
        if any(frame.function == "_record_review_result" for frame in inspect.stack()[:12]):
            time.sleep(0.05)
        return orig(value)

    pipeline_mod.as_list = slow_as_list
    try:
        return fn()
    finally:
        pipeline_mod.as_list = orig


def _review_payload(title):
    return {
        "summary": title,
        "findings": [_finding(title, kind="implementation")],
        "review_markdown": "# %s\n" % title,
    }


class DualReviewPinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _both_runtime(self):
        return HostileRuntime(
            by_phase={
                "reviewer-claude": [emit(_review_payload("A"))],
                "reviewer-grok": [emit(_review_payload("B"))],
            },
            num_turns=2,
        )

    def _enable_both(self, pipe):
        pipe.cfg.fake = False
        pipe.cfg.roles["reviewer"] = "both"

    def _assert_both_pins(self, pipe):
        claude = pipe.work / "prompts" / "reviewer-claude.result.json"
        grok = pipe.work / "prompts" / "reviewer-grok.result.json"
        self.assertTrue(claude.is_file(), "claude result missing")
        self.assertTrue(grok.is_file(), "grok result missing")
        results = list((pipe.state.last_review or {}).get("results") or [])
        names = {row.get("name") for row in results}
        self.assertEqual(names, {claude.name, grok.name})
        by_name = {row["name"]: row for row in results}
        for path in (claude, grok):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(by_name[path.name]["digest"], digest)
        found = collect_review_findings(pipe.work)
        self.assertEqual({item["title"] for item in found}, {"A", "B"})
        review = (pipe.work / "review.md").read_text(encoding="utf-8")
        self.assertIn("A", review)
        self.assertIn("B", review)

    def _run_both(self, fn):
        """Widen the unlocked pin RMW window without deadlocking a lock."""
        return _with_slow_record_as_list(fn)

    def test_feature_reviewer_both_parallel_keeps_both_pins(self):
        cfg = load_config(self.repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_feature(cfg, "brief", "both-feature")
        self._enable_both(pipe)
        hostile = self._both_runtime()
        with register_runtime(("claude", "grok"), hostile):
            self._run_both(pipe.phase_reviewer)
        self._assert_both_pins(pipe)

    def test_range_reviewer_both_parallel_keeps_both_pins(self):
        cfg = load_config(self.repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_range_review(cfg, slug="both-range")
        self._enable_both(pipe)
        hostile = self._both_runtime()
        with register_runtime(("claude", "grok"), hostile):
            self._run_both(pipe.phase_range_reviewer)
        self._assert_both_pins(pipe)

    def test_status_reviewer_both_parallel_keeps_both_pins(self):
        cfg = load_config(self.repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_audit(cfg, "status?", "both-audit")
        self._enable_both(pipe)
        hostile = self._both_runtime()
        with register_runtime(("claude", "grok"), hostile):
            self._run_both(pipe.phase_status_reviewer)
        self._assert_both_pins(pipe)

    def test_overlapping_record_review_result_keeps_both_names(self):
        from concurrent.futures import ThreadPoolExecutor

        cfg = load_config(self.repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_feature(cfg, "brief", "overlap-record")
        pipe._begin_review_attempt()
        a = pipe.work / "prompts" / "reviewer-claude.result.json"
        b = pipe.work / "prompts" / "reviewer-grok.result.json"
        dump_json(a, _review_payload("A"))
        dump_json(b, _review_payload("B"))

        def race():
            with ThreadPoolExecutor(max_workers=2) as pool:
                futs = [
                    pool.submit(pipe._record_review_result, a),
                    pool.submit(pipe._record_review_result, b),
                ]
                for fut in futs:
                    fut.result()

        _with_slow_record_as_list(race)
        names = {row.get("name") for row in (pipe.state.last_review or {}).get("results") or []}
        self.assertEqual(names, {a.name, b.name})
        by_name = {
            row["name"]: row for row in (pipe.state.last_review or {}).get("results") or []
        }
        self.assertEqual(by_name[a.name]["digest"], hashlib.sha256(a.read_bytes()).hexdigest())
        self.assertEqual(by_name[b.name]["digest"], hashlib.sha256(b.read_bytes()).hexdigest())


class ResultPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def test_invoke_result_json_round_trips_payload_larger_than_200k(self):
        import json

        from team.pipeline import start_feature

        cfg = load_config(self.repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_feature(cfg, "brief", "big-review")
        findings = [
            _finding("finding-%02d" % i, kind="implementation", path="src/f%02d.py" % i)
            for i in range(10)
        ]
        payload = {
            "summary": "large",
            "findings": findings,
            "review_markdown": "M" * 210000,
        }
        self.assertGreater(len(json.dumps(payload, indent=2)), 200000)
        hostile = HostileRuntime([emit(payload)], phases=("reviewer-fake",), num_turns=2)
        with register_runtime("fake", hostile):
            pipe.phase_reviewer()
        result_path = pipe.work / "prompts" / "reviewer-fake.result.json"
        try:
            data = load_json(result_path)
        except Exception as exc:
            self.fail(
                "result JSON must round-trip a >200k payload, got %s"
                % exc
            )
        titles = [item["title"] for item in data.get("findings") or []]
        self.assertEqual(len(titles), 10)
        for i in range(10):
            self.assertIn("finding-%02d" % i, titles)
        digest = hashlib.sha256(result_path.read_bytes()).hexdigest()
        recorded = (pipe.state.last_review or {}).get("results") or []
        self.assertEqual(recorded[0]["digest"], digest)
        found = collect_review_findings(pipe.work)
        self.assertEqual({item["title"] for item in found}, set(titles))

    def test_collect_and_apply_fail_closed_on_unparseable_recorded_pin(self):
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
        work = self.repo / ".team" / "work" / "add-greet-helper"
        (work / "review.md").write_text("# Review\n", encoding="utf-8")
        pin = work / "prompts" / "reviewer-fake.result.json"
        pin.write_text('{"summary": "trunc", "findings": [', encoding="utf-8")
        digest = hashlib.sha256(pin.read_bytes()).hexdigest()
        state = State.load(work)
        rec = dict(state.last_review or {})
        rec["results"] = [{"name": pin.name, "digest": digest}]
        state.last_review = rec
        state.save(work)
        with self.assertRaises(Exception):
            collect_review_findings(work)
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "add-greet-helper",
            ]
        )
        self.assertNotEqual(rc, 0)
        self.assertNotEqual(State.load(work).stop_reason, "applied")
        self.assertFalse((work / "apply-impl-summary.md").is_file())


class MergeOverlapTests(unittest.TestCase):
    def test_single_reviewer_does_not_claim_no_shared_hits(self):
        md = merge_reviews(
            [
                (
                    "grok",
                    {
                        "summary": "S",
                        "findings": [_finding("only", kind="note")],
                    },
                    "body",
                )
            ]
        )
        self.assertNotIn("no shared path+title hits", md)

    def test_two_reviewers_overlap_still_reported(self):
        finding = _finding("leak", kind="implementation")
        md = merge_reviews(
            [
                ("claude", {"summary": "A", "findings": [finding]}, "a"),
                ("grok", {"summary": "B", "findings": [finding]}, "b"),
            ]
        )
        self.assertIn("src/a.py", md)
        self.assertIn("leak", md.lower())


if __name__ == "__main__":
    unittest.main()
