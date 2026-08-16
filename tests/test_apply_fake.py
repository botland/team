import json
import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.cli import main
from team.config import load_config
from team.pipeline import start_feature
from team.findings import (
    collect_guardian_findings,
    collect_review_findings,
    empty_seq_state,
    finding_id,
    mark_seq_step,
    pick_next_seq,
    related_guardian,
    seq_candidates,
)
from team.state import State
from tests.support.hostile import HostileRuntime, emit, register_runtime

_ACTIONABLE = frozenset(("architecture", "implementation", "test"))


def _git_repo(root: Path) -> None:
    subprocess.check_call(["git", "init"], cwd=str(root), stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "config", "user.email", "t@t"], cwd=str(root))
    subprocess.check_call(["git", "config", "user.name", "t"], cwd=str(root))
    (root / "README").write_text("x\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "README"], cwd=str(root))
    subprocess.check_call(["git", "commit", "-m", "init"], cwd=str(root), stdout=subprocess.DEVNULL)


def _inject_findings(work: Path, findings) -> None:
    path = work / "prompts" / "reviewer-fake.result.json"
    path.write_text(json.dumps({"findings": findings, "summary": "injected"}), encoding="utf-8")


def _write_guardian(work: Path, *, risks, chain, finished=True, num_turns=2) -> None:
    (work / "prompts").mkdir(parents=True, exist_ok=True)
    payload = {
        "risks": risks,
        "guardian_markdown": "injected",
        "chain": chain,
    }
    if finished:
        payload["num_turns"] = num_turns
        payload["_meta"] = {"role": "guardian", "phase": "guardian", "num_turns": num_turns}
    (work / "prompts" / "guardian.result.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _seq_state(work: Path):
    return json.loads((work / "findings.json").read_text(encoding="utf-8"))["seq"]


def _last_applied_hops(seq, fid):
    hops = []
    for step in seq.get("steps") or []:
        if step.get("id") == fid and step.get("status") == "applied":
            hops = list(step.get("hops") or [])
    return hops


def _assert_no_related_apply(test, seq):
    for step in seq.get("steps") or []:
        hops = list(step.get("hops") or [])
        test.assertNotEqual(
            hops,
            ["related"],
            msg="id %s must not be applied as related" % step.get("id"),
        )
        test.assertNotIn("related", hops, msg="id %s hop token related" % step.get("id"))


def _pool_remainder(work, seq, *, treat_stale_as_done=False):
    pool = collect_review_findings(work) + collect_guardian_findings(work)
    actionable = {
        finding_id(item)
        for item in pool
        if (item.get("kind") or "") in _ACTIONABLE
    }
    done = set(seq.get("applied") or []) | set(seq.get("skipped") or [])
    if treat_stale_as_done:
        done |= set(seq.get("stale") or [])
    return actionable - done


def _assert_stop_applied_exhausted(test, work, seq):
    """stop_reason=applied is legal only when the collected pool is exhausted.

    Stale is temporary only while resume/failed is set. An empty resume+failed
    with leftover stale is a dead-letter suffix, not a finished queue.
    """
    leftover = _pool_remainder(work, seq, treat_stale_as_done=False)
    unresolved = bool(seq.get("resume") or seq.get("failed"))
    if not unresolved:
        test.assertEqual(
            set(seq.get("stale") or []),
            set(),
            "stale suffix while stop_reason=applied and prefix is resolved",
        )
        test.assertEqual(leftover, set(), leftover)
    else:
        test.assertEqual(
            leftover - set(seq.get("stale") or []),
            set(),
            leftover,
        )


class FakeApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _git_repo(self.repo)
        os.environ["TEAM_HOME"] = str(Path(__file__).resolve().parents[1])

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

    def test_apply_implementation_finding(self):
        work = self._feature()
        design_before = (work / "design.md").read_text(encoding="utf-8")
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
        )
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
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
        out = buf.getvalue()
        self.assertIn("greet ignores empty name", out)
        self.assertIn("src/greet.py", out)
        self.assertTrue((work / "apply-plan.md").is_file())
        self.assertTrue((work / "apply-summary.md").is_file())
        self.assertTrue((work / "apply-impl-summary.md").is_file())
        self.assertTrue((work / "apply-test-report.md").is_file())
        self.assertTrue((work / "findings.json").is_file())
        plan = (work / "apply-plan.md").read_text(encoding="utf-8")
        self.assertIn("implementation (1)", plan)
        self.assertNotIn("Unchanged assumptions", (work / "design.md").read_text(encoding="utf-8"))
        self.assertEqual((work / "design.md").read_text(encoding="utf-8"), design_before)
        state = State.load(work)
        self.assertEqual(state.stop_reason, "applied")

    def test_apply_implementation_on_already_dirty_code_is_ok(self):
        from tests.support.hostile import HostileRuntime, emit, register_runtime, write
        from tests.support.verify_report import (
            ALREADY_DIRTY_HEADING,
            heading_paths,
            has_heading,
        )

        (self.repo / "src").mkdir(exist_ok=True)
        (self.repo / "src" / "greet.py").write_text("# user wip\n", encoding="utf-8")
        work = self._feature()
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
        )
        hostile = HostileRuntime(
            [
                write("src/greet.py", "# user wip\npatched\n"),
                emit({"summary": "patched", "paths_touched": ["src/greet.py"]}),
            ],
            phases=("implementer-apply",),
        )
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
        self.assertEqual(State.load(work).stop_reason, "applied")
        apply_verify = (work / "git" / "verify-apply-implementer.md").read_text(encoding="utf-8")
        self.assertTrue(has_heading(apply_verify, ALREADY_DIRTY_HEADING), apply_verify)
        self.assertIn("src/greet.py", heading_paths(apply_verify, ALREADY_DIRTY_HEADING))
        self.assertNotIn("src/greet.py", heading_paths(apply_verify, "violations:"))

    def test_apply_test_finding_does_not_replan(self):
        work = self._feature()
        _inject_findings(
            work,
            [
                {
                    "severity": "medium",
                    "title": "missing empty-name test",
                    "evidence": "contract omits reject case",
                    "path": "tests/test_greet.py",
                    "kind": "test",
                }
            ],
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
        self.assertEqual(rc, 0)
        self.assertTrue((work / "apply-tdd-summary.md").is_file())
        self.assertFalse((work / "design-replan.md").is_file())
        self.assertIn("test (1)", (work / "apply-plan.md").read_text(encoding="utf-8"))

    def test_apply_architecture_replans(self):
        work = self._feature()
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "wrong module boundary",
                    "evidence": "greet in the wrong package",
                    "path": "src/greet.py",
                    "kind": "architecture",
                }
            ],
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
        self.assertEqual(rc, 0)
        self.assertTrue((work / "design-replan.md").is_file())
        design = (work / "design.md").read_text(encoding="utf-8")
        delta = (work / "design-replan.md").read_text(encoding="utf-8")
        self.assertIn("Unchanged assumptions", design)
        self.assertIn("Unchanged assumptions", delta)
        self.assertIn("no network", design)
        self.assertNotIn("no network", delta)
        self.assertNotEqual(design, delta)
        summary = (work / "apply-summary.md").read_text(encoding="utf-8")
        self.assertIn("architect replan", summary)
        self.assertIn("implementer", summary)
        self.assertIn("test-writer", summary)

    def test_apply_debugger_lists_apply_test_report(self):
        work = self._feature()
        (work / "test-report.md").write_text(
            "STALE_FEATURE_SUITE_IDENTITY\n", encoding="utf-8"
        )
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "false",
                "apply",
                "--repair",
                "add-greet-helper",
            ]
        )
        self.assertTrue((work / "apply-test-report.md").is_file(), rc)
        dbg = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (work / "prompts").glob("*debugger*")
        )
        self.assertTrue(dbg.strip(), "debugger must have been invoked")
        self.assertIn("apply-test-report.md", dbg)
        repair = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (work / "prompts").glob("*repair*")
        )
        if repair.strip():
            self.assertIn("apply-test-report.md", repair)

    def test_apply_without_repair_flag_stops_at_needs_repair(self):
        work = self._feature()
        (work / "test-report.md").write_text(
            "STALE_FEATURE_SUITE_IDENTITY\n", encoding="utf-8"
        )
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "false",
                "apply",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue((work / "apply-test-report.md").is_file())
        for pattern in ("*debugger*", "*repair*"):
            hits = "\n".join(
                p.read_text(encoding="utf-8")
                for p in (work / "prompts").glob(pattern)
            )
            self.assertFalse(hits.strip(), "%s must be opt-in on apply" % pattern)
        self.assertEqual(State.load(work).stop_reason, "needs-repair")
        summary = (work / "apply-summary.md").read_text(encoding="utf-8")
        self.assertIn("debug/repair off", summary)

    def test_apply_unclassified_stops_without_review(self):
        work = self._feature()
        review_before = (work / "review.md").read_text(encoding="utf-8")
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "something wrong",
                    "evidence": "see code",
                    "path": "src/greet.py",
                }
            ],
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
        self.assertEqual(rc, 0)
        plan = (work / "apply-plan.md").read_text(encoding="utf-8")
        self.assertIn("unclassified", plan.lower())
        self.assertEqual(State.load(work).stop_reason, "needs-classification")
        self.assertEqual((work / "review.md").read_text(encoding="utf-8"), review_before)
        self.assertFalse((work / "apply-impl-summary.md").is_file())

    def test_apply_dry_run_writes_plan_only(self):
        work = self._feature()
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "apply",
                "--dry-run",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue((work / "apply-plan.md").is_file())
        self.assertFalse((work / "apply-test-report.md").is_file())
        self.assertFalse((work / "apply-impl-summary.md").is_file())
        self.assertEqual(State.load(work).stop_reason, "dry_run")

    def test_apply_plan_includes_guardian_risks_when_reviewer_is_minor(self):
        """Apply queues guardian risks even when the reviewer only logged notes
        and the guardian artifact has no persisted num_turns."""
        work = self._feature()
        _inject_findings(
            work,
            [
                {
                    "severity": "medium",
                    "title": "One listing-timeout knob, three meanings",
                    "evidence": "four homes",
                    "path": "src/a.py",
                    "kind": "architecture",
                },
                {
                    "severity": "medium",
                    "title": "Documented-knob test never checks what the knob teaches",
                    "evidence": "key present",
                    "path": "tests/test_a.py",
                    "kind": "test",
                },
                {
                    "severity": "medium",
                    "title": "Range review is commit-only",
                    "evidence": "empty diff",
                    "path": ".team/work/x/git/start.txt",
                    "kind": "note",
                },
            ],
        )
        _write_guardian(
            work,
            risks=[
                {
                    "title": "Whole-phase bound is an approximation",
                    "evidence": "httpx has no total-request timeout",
                    "path": "src/a.py",
                    "link": "invariant",
                },
                {
                    "title": "The last useful second branch is float equality",
                    "evidence": "hop == MIN_USEFUL_BUDGET_SEC",
                    "path": "src/a.py",
                    "link": "t_to_i",
                },
                {
                    "title": "ListingStop totality is a runtime raise",
                    "evidence": "TypeError inside the chat turn",
                    "path": "src/a.py",
                    "link": "a_to_t",
                },
            ],
            chain={
                "r_to_a": {"ok": False, "note": "adr silent"},
                "a_to_t": {"ok": False, "note": "map not total"},
                "t_to_i": {"ok": False, "note": "float eq"},
                "i_to_r": {"ok": False, "note": "starvation"},
            },
            finished=False,
        )
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "apply",
                    "--dry-run",
                    "add-greet-helper",
                ]
            )
        self.assertEqual(rc, 0, buf.getvalue())
        out = buf.getvalue()
        plan = (work / "apply-plan.md").read_text(encoding="utf-8")
        data = json.loads((work / "findings.json").read_text(encoding="utf-8"))
        guardian_rows = [
            row for row in data["findings"] if row.get("source") == "guardian"
        ]
        self.assertEqual(len(guardian_rows), 3, data["findings"])
        kinds = {row["kind"] for row in guardian_rows}
        self.assertEqual(kinds, {"architecture", "implementation", "test"})
        self.assertIn("Whole-phase bound is an approximation", plan)
        self.assertIn("float equality", plan)
        self.assertIn("ListingStop totality", plan)
        self.assertIn("implementation (1)", plan)
        self.assertNotIn("implementation (0)", plan)
        self.assertIn("apply plan:", out)
        self.assertRegex(out, r"arch=\d+ impl=1 test=\d+")
        self.assertNotRegex(out, r"impl=0")

    def test_apply_refuses_audit(self):
        self.assertEqual(
            main(["--repo", str(self.repo), "--fake", "audit", "--force"]),
            0,
        )
        rc = main(["--repo", str(self.repo), "--fake", "apply", "audit"])
        self.assertEqual(rc, 1)

    def test_apply_needs_review(self):
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "feature",
                "--dry-run",
                "Add greet helper",
            ]
        )
        self.assertEqual(rc, 0)
        rc = main(["--repo", str(self.repo), "--fake", "apply", "add-greet-helper"])
        self.assertEqual(rc, 1)

    def test_apply_needs_review_after_complete_feature(self):
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
        self.assertFalse((work / "review.md").is_file())
        rc = main(["--repo", str(self.repo), "--fake", "apply", "add-greet-helper"])
        self.assertEqual(rc, 1)

    def test_seq_applies_guardian_when_review_is_notes_only(self):
        rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "review-since-tag"
        _write_guardian(
            work,
            risks=[
                {
                    "title": "slash still taught as legal",
                    "evidence": "error names slash",
                    "path": "README",
                    "link": "t_to_i",
                }
            ],
            chain={
                "r_to_a": {"ok": True, "note": "n"},
                "a_to_t": {"ok": True, "note": "n"},
                "t_to_i": {"ok": False, "note": "n"},
                "i_to_r": {"ok": True, "note": "n"},
            },
        )
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--code-root",
                    ".",
                    "--test-command",
                    "true",
                    "apply",
                    "--seq",
                    "review-since-tag",
                ]
            )
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("apply: implementer (fake) …", out)
        self.assertIn("apply: host suite (host) …", out)
        self.assertLess(out.find("apply: implementer"), out.find("apply: implementation"))
        log = (work / "apply-seq.md").read_text(encoding="utf-8")
        self.assertIn("slash still taught as legal", log)
        self.assertIn("applied", log.lower())
        self.assertEqual(State.load(work).stop_reason, "applied")
        self.assertTrue(list((work / "seq").iterdir()))
        seq = _seq_state(work)
        _assert_no_related_apply(self, seq)
        guardian_rows = collect_guardian_findings(work)
        self.assertEqual(len(guardian_rows), 1)
        gid = finding_id(guardian_rows[0])
        hops = _last_applied_hops(seq, gid)
        self.assertTrue(any("implementer" in hop for hop in hops), hops)
        self.assertFalse(any(hop == "related" for hop in hops), hops)
        self.assertIn(gid, seq["applied"])
        _assert_stop_applied_exhausted(self, work, seq)

    def test_apply_range_implementation(self):
        rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "review-since-tag"
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "readme typo as stand-in bug",
                    "evidence": "README",
                    "path": "README",
                    "kind": "implementation",
                }
            ],
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--code-root",
                ".",
                "--test-command",
                "true",
                "apply",
                "review-since-tag",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue((work / "apply-impl-summary.md").is_file())
        self.assertEqual(State.load(work).stop_reason, "applied")

    def _inject_mixed(self, work):
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                },
                {
                    "severity": "high",
                    "title": "wrong module boundary",
                    "evidence": "greet in the wrong package",
                    "path": "src/greet.py",
                    "kind": "architecture",
                },
            ],
        )

    def test_seq_arch_before_impl_and_preserves_review(self):
        work = self._feature()
        review_before = (work / "review.md").read_text(encoding="utf-8")
        self._inject_mixed(work)
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual((work / "review.md").read_text(encoding="utf-8"), review_before)
        log = (work / "apply-seq.md").read_text(encoding="utf-8")
        impl_at = log.find("greet ignores empty name")
        arch_at = log.find("wrong module boundary")
        self.assertGreater(arch_at, 0)
        self.assertGreater(impl_at, arch_at)
        self.assertIn("architect replan", log)
        self.assertTrue((work / "design-replan.md").is_file())
        design = (work / "design.md").read_text(encoding="utf-8")
        delta = (work / "design-replan.md").read_text(encoding="utf-8")
        self.assertIn("no network", design)
        self.assertNotIn("no network", delta)
        impl_prompts = list((work / "prompts").glob("*implementer-apply*"))
        impl_text = "\n".join(p.read_text(encoding="utf-8") for p in impl_prompts)
        self.assertIn("design.md", impl_text)
        # design.md is this role's own input, small enough to carry.
        self.assertIn(design.strip(), impl_text)
        seq_dirs = list((work / "seq").iterdir())
        self.assertEqual(len(seq_dirs), 2)
        reviews = list((work / "seq").glob("*/review.md"))
        self.assertEqual(len(reviews), 0)
        self.assertEqual(State.load(work).stop_reason, "applied")

    def test_seq_dry_run_does_not_implement(self):
        work = self._feature()
        self._inject_mixed(work)
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "apply",
                "--seq",
                "--dry-run",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        plan = (work / "apply-plan.md").read_text(encoding="utf-8")
        self.assertIn("greet ignores empty name", plan)
        self.assertIn("wrong module boundary", plan)
        self.assertFalse((work / "apply-impl-summary.md").is_file())
        self.assertFalse((work / "seq").exists())
        self.assertEqual(State.load(work).stop_reason, "dry_run")

    def test_seq_stops_on_suite_failure_and_retries_same_class(self):
        work = self._feature()
        review_before = (work / "review.md").read_text(encoding="utf-8")
        self._inject_mixed(work)
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "false",
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(State.load(work).stop_reason, "seq-failed")
        self.assertEqual((work / "review.md").read_text(encoding="utf-8"), review_before)
        log = (work / "apply-seq.md").read_text(encoding="utf-8")
        self.assertIn("wrong module boundary", log)
        self.assertNotIn("greet ignores empty name", log)
        self.assertIn("Stopped", log)
        data = json.loads((work / "findings.json").read_text(encoding="utf-8"))
        failed = data["seq"]["failed"]
        self.assertTrue(failed)
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "false",
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 1)
        data2 = json.loads((work / "findings.json").read_text(encoding="utf-8"))
        self.assertEqual(data2["seq"]["failed"], failed)
        self.assertEqual(data2["seq"]["steps"][-1]["id"], failed)

    def test_seq_skip_failed_continues(self):
        work = self._feature()
        self._inject_mixed(work)
        self.assertEqual(
            main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--test-command",
                    "false",
                    "apply",
                    "--seq",
                    "add-greet-helper",
                ]
            ),
            1,
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "--seq",
                "--skip-failed",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        log = (work / "apply-seq.md").read_text(encoding="utf-8")
        self.assertIn("skipped", log)
        self.assertIn("greet ignores empty name", log)
        self.assertEqual(State.load(work).stop_reason, "applied")

    def test_review_seq_does_not_touch_review_md(self):
        work = self._feature()
        self._inject_mixed(work)
        self.assertEqual(
            main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--test-command",
                    "true",
                    "apply",
                    "--seq",
                    "add-greet-helper",
                ]
            ),
            0,
        )
        review_before = (work / "review.md").read_text(encoding="utf-8")
        steps = list((work / "seq").iterdir())
        self.assertTrue(steps)
        rc = main(
            ["--repo", str(self.repo), "--fake", "review", "add-greet-helper", "--seq"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual((work / "review.md").read_text(encoding="utf-8"), review_before)
        self.assertTrue((work / "seq" / steps[0].name / "review.md").is_file() or any(
            (p / "review.md").is_file() for p in (work / "seq").iterdir()
        ))

    def test_seq_writes_checkpoint(self):
        work = self._feature()
        self._inject_mixed(work)
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        checks = list((work / "seq").glob("*/checkpoint.json"))
        self.assertEqual(len(checks), 2)
        data = json.loads(checks[0].read_text(encoding="utf-8"))
        self.assertIn("head_before", data)
        self.assertIn("touched", data)
        self.assertIn("suite", data)

    def test_seq_reopen_dry_run_reports_but_does_not_touch_the_queue(self):
        """Bug 6: --dry-run performed the reopen.

        The product tree is not the only state --dry-run must leave alone. The
        seq queue is what the next --seq consumes, so marking a suffix stale is
        a real edit to it: the run says "nothing was changed" and the next
        apply then redoes a class it already applied.
        """
        work = self._feature()
        self._inject_mixed(work)
        self.assertEqual(
            main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--test-command",
                    "true",
                    "apply",
                    "--seq",
                    "add-greet-helper",
                ]
            ),
            0,
        )
        before = (work / "findings.json").read_text(encoding="utf-8")
        first = json.loads(before)["seq"]["steps"][0]["id"]
        followups_before = (work / "followups.md").read_text(encoding="utf-8")
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "apply",
                    "--seq",
                    "--reopen",
                    first,
                    "--dry-run",
                    "add-greet-helper",
                ]
            )
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertEqual(
            (work / "findings.json").read_text(encoding="utf-8"),
            before,
            "--dry-run must not rewrite the seq queue",
        )
        self.assertEqual(
            (work / "followups.md").read_text(encoding="utf-8"), followups_before
        )
        self.assertFalse((work / "seq" / first / "reopen.md").is_file())
        self.assertEqual(State.load(work).stop_reason, "dry_run")
        self.assertIn("would reopen", out)
        self.assertIn(first, out)

    def test_seq_reopen_dry_run_still_refuses_an_unknown_id(self):
        """Validation is not skipped by --dry-run: a bad id is still an error."""
        work = self._feature()
        self._inject_mixed(work)
        self.assertEqual(
            main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--test-command",
                    "true",
                    "apply",
                    "--seq",
                    "add-greet-helper",
                ]
            ),
            0,
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "apply",
                "--seq",
                "--reopen",
                "no-such-id",
                "--dry-run",
                "add-greet-helper",
            ]
        )
        self.assertNotEqual(rc, 0)

    def test_seq_reopen_and_list_show_status(self):
        work = self._feature()
        self._inject_mixed(work)
        self.assertEqual(
            main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--test-command",
                    "true",
                    "apply",
                    "--seq",
                    "add-greet-helper",
                ]
            ),
            0,
        )
        data = json.loads((work / "findings.json").read_text(encoding="utf-8"))
        first = data["seq"]["steps"][0]["id"]
        second = data["seq"]["steps"][1]["id"]
        review_before = (work / "review.md").read_text(encoding="utf-8")
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "apply",
                    "--seq",
                    "--reopen",
                    first,
                    "add-greet-helper",
                ]
            )
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual(State.load(work).stop_reason, "seq-reopened")
        self.assertEqual((work / "review.md").read_text(encoding="utf-8"), review_before)
        self.assertTrue((work / "seq" / first / "reopen.md").is_file())
        seq = json.loads((work / "findings.json").read_text(encoding="utf-8"))["seq"]
        self.assertEqual(seq["resume"], first)
        self.assertIn(second, seq["stale"])
        listed = StringIO()
        with mock.patch("sys.stdout", listed):
            self.assertEqual(main(["--repo", str(self.repo), "list"]), 0)
        text = listed.getvalue()
        self.assertIn(first, text)
        self.assertIn("reopened", text)
        self.assertIn(second, text)
        self.assertIn("stale", text)
        self.assertNotIn("closed", text)
        self.assertNotIn("excluded", text)
        status = StringIO()
        with mock.patch("sys.stdout", status):
            self.assertEqual(
                main(["--repo", str(self.repo), "status", "add-greet-helper"]), 0
            )
        status_text = status.getvalue()
        self.assertIn(first, status_text)
        self.assertIn(second, status_text)
        self.assertIn("stale", status_text)
        self.assertIn("reopened", status_text)
        reopen_md = (work / "seq" / first / "reopen.md").read_text(encoding="utf-8")
        self.assertIn(second, reopen_md)
        self.assertIn("stale", reopen_md.lower())
        self.assertNotIn("skipped", reopen_md.lower())
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        seq2 = _seq_state(work)
        self.assertNotIn(second, seq2["stale"])
        self.assertIn(first, seq2["applied"])
        self.assertIn(second, seq2["applied"])
        self.assertNotEqual(_last_applied_hops(seq2, second), ["related"])
        self.assertTrue(
            any("implementer" in hop for hop in _last_applied_hops(seq2, second)),
            _last_applied_hops(seq2, second),
        )
        _assert_no_related_apply(self, seq2)
        self.assertEqual(State.load(work).stop_reason, "applied")
        _assert_stop_applied_exhausted(self, work, seq2)
        listed2 = StringIO()
        with mock.patch("sys.stdout", listed2):
            self.assertEqual(main(["--repo", str(self.repo), "list"]), 0)
        after = listed2.getvalue()
        self.assertIn(first, after)
        self.assertIn(second, after)
        self.assertNotIn("stale", after)

    def test_seq_test_class_does_not_apply_same_path_guardian_t_to_i(self):
        work = self._feature()
        test_row = {
            "severity": "high",
            "title": "missing empty-name test",
            "evidence": "contract omits reject case",
            "path": "src/greet.py",
            "kind": "test",
        }
        _inject_findings(work, [test_row])
        _write_guardian(
            work,
            risks=[
                {
                    "title": "greet still accepts empty",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "link": "t_to_i",
                }
            ],
            chain={
                "r_to_a": {"ok": True, "note": "n"},
                "a_to_t": {"ok": True, "note": "n"},
                "t_to_i": {"ok": False, "note": "n"},
                "i_to_r": {"ok": True, "note": "n"},
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
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        review_id = finding_id(collect_review_findings(work)[0])
        guardian_id = finding_id(collect_guardian_findings(work)[0])
        seq = _seq_state(work)
        _assert_no_related_apply(self, seq)
        self.assertIn(review_id, seq["applied"])
        self.assertIn(guardian_id, seq["applied"])
        test_hops = _last_applied_hops(seq, review_id)
        guard_hops = _last_applied_hops(seq, guardian_id)
        self.assertTrue(any("tdd-design" in hop for hop in test_hops), test_hops)
        self.assertTrue(any("test-writer" in hop for hop in test_hops), test_hops)
        self.assertFalse(any("implementer" in hop for hop in test_hops), test_hops)
        self.assertTrue(any("implementer" in hop for hop in guard_hops), guard_hops)
        self.assertNotEqual(guard_hops, ["related"])
        applied_order = [
            step["id"]
            for step in seq["steps"]
            if step.get("status") == "applied" and step.get("id") in (review_id, guardian_id)
        ]
        self.assertLess(applied_order.index(review_id), applied_order.index(guardian_id))
        self.assertEqual(State.load(work).stop_reason, "applied")
        _assert_stop_applied_exhausted(self, work, seq)
        self.assertTrue((work / "seq" / review_id).is_dir())
        self.assertTrue((work / "seq" / guardian_id).is_dir())

    def test_two_guardian_links_on_same_path_are_two_classes(self):
        work = self._feature()
        _write_guardian(
            work,
            risks=[
                {
                    "title": "contract hole",
                    "evidence": "no reject case",
                    "path": "src/greet.py",
                    "link": "a_to_t",
                },
                {
                    "title": "slash still taught as legal",
                    "evidence": "error names slash",
                    "path": "src/greet.py",
                    "link": "t_to_i",
                },
            ],
            chain={
                "r_to_a": {"ok": True, "note": "n"},
                "a_to_t": {"ok": False, "note": "n"},
                "t_to_i": {"ok": False, "note": "n"},
                "i_to_r": {"ok": True, "note": "n"},
            },
        )
        rows = collect_guardian_findings(work)
        by_link = {}
        for row in rows:
            if "a_to_t" in row["title"]:
                by_link["a_to_t"] = row
            if "t_to_i" in row["title"]:
                by_link["t_to_i"] = row
        test_row = by_link["a_to_t"]
        impl_row = by_link["t_to_i"]
        test_id = finding_id(test_row)
        impl_id = finding_id(impl_row)
        self.assertNotEqual(test_id, impl_id)
        mid = mark_seq_step(
            empty_seq_state(),
            dict(test_row, id=test_id),
            status="applied",
            hops=["tdd-design contract", "test-writer", "suite PASS"],
        )
        self.assertIn(test_id, mid["applied"])
        self.assertNotIn(impl_id, mid["applied"])
        self.assertNotIn(impl_id, mid["skipped"])
        self.assertNotIn(impl_id, mid["stale"])
        mid_ids = [row["id"] for row in seq_candidates([test_row, impl_row], mid)]
        self.assertEqual(mid_ids, [impl_id])
        self.assertEqual(pick_next_seq([test_row, impl_row], mid)["id"], impl_id)
        self.assertTrue(any(finding_id(row) == impl_id for row in related_guardian(test_row, rows)))
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        seq = _seq_state(work)
        _assert_no_related_apply(self, seq)
        self.assertIn(test_id, seq["applied"])
        self.assertIn(impl_id, seq["applied"])
        test_hops = _last_applied_hops(seq, test_id)
        impl_hops = _last_applied_hops(seq, impl_id)
        self.assertTrue(any("tdd-design" in hop for hop in test_hops), test_hops)
        self.assertTrue(any("test-writer" in hop for hop in test_hops), test_hops)
        self.assertFalse(any("implementer" in hop for hop in test_hops), test_hops)
        self.assertTrue(any("implementer" in hop for hop in impl_hops), impl_hops)
        self.assertFalse(any("tdd-design" in hop for hop in impl_hops), impl_hops)
        applied_order = [
            step["id"]
            for step in seq["steps"]
            if step.get("status") == "applied" and step.get("id") in (test_id, impl_id)
        ]
        self.assertLess(applied_order.index(test_id), applied_order.index(impl_id))
        self.assertEqual(State.load(work).stop_reason, "applied")
        _assert_stop_applied_exhausted(self, work, seq)

    def test_seq_review_and_two_guardians_are_three_classes(self):
        work = self._feature()
        review_row = {
            "severity": "high",
            "title": "missing empty-name test",
            "evidence": "contract omits reject case",
            "path": "src/greet.py",
            "kind": "test",
        }
        _inject_findings(work, [review_row])
        _write_guardian(
            work,
            risks=[
                {
                    "title": "contract hole",
                    "evidence": "no reject case",
                    "path": "src/greet.py",
                    "link": "a_to_t",
                },
                {
                    "title": "slash still taught as legal",
                    "evidence": "error names slash",
                    "path": "src/greet.py",
                    "link": "t_to_i",
                },
            ],
            chain={
                "r_to_a": {"ok": True, "note": "n"},
                "a_to_t": {"ok": False, "note": "n"},
                "t_to_i": {"ok": False, "note": "n"},
                "i_to_r": {"ok": True, "note": "n"},
            },
        )
        guardian_rows = collect_guardian_findings(work)
        a_to_t = next(row for row in guardian_rows if "a_to_t" in row["title"])
        t_to_i = next(row for row in guardian_rows if "t_to_i" in row["title"])
        review_id = finding_id(review_row)
        a_id = finding_id(a_to_t)
        i_id = finding_id(t_to_i)
        self.assertEqual(len({review_id, a_id, i_id}), 3)
        mid = mark_seq_step(
            empty_seq_state(),
            dict(review_row, id=review_id),
            status="applied",
            hops=["tdd-design contract", "test-writer", "suite PASS"],
        )
        leftover = [row["id"] for row in seq_candidates([review_row, a_to_t, t_to_i], mid)]
        self.assertNotIn(review_id, leftover)
        self.assertIn(a_id, leftover)
        self.assertIn(i_id, leftover)
        self.assertNotIn(a_id, mid["applied"])
        self.assertNotIn(i_id, mid["applied"])
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        seq = _seq_state(work)
        _assert_no_related_apply(self, seq)
        for fid in (review_id, a_id, i_id):
            self.assertIn(fid, seq["applied"])
            self.assertTrue((work / "seq" / fid).is_dir())
        review_hops = _last_applied_hops(seq, review_id)
        a_hops = _last_applied_hops(seq, a_id)
        i_hops = _last_applied_hops(seq, i_id)
        self.assertTrue(any("tdd-design" in hop for hop in review_hops), review_hops)
        self.assertFalse(any("implementer" in hop for hop in review_hops), review_hops)
        self.assertTrue(any("tdd-design" in hop for hop in a_hops), a_hops)
        self.assertFalse(any("implementer" in hop for hop in a_hops), a_hops)
        self.assertTrue(any("implementer" in hop for hop in i_hops), i_hops)
        applied_ids = [
            step["id"]
            for step in seq["steps"]
            if step.get("status") == "applied" and step.get("id") in {review_id, a_id, i_id}
        ]
        self.assertLess(max(applied_ids.index(review_id), applied_ids.index(a_id)), applied_ids.index(i_id))
        self.assertEqual(State.load(work).stop_reason, "applied")
        _assert_stop_applied_exhausted(self, work, seq)

    def test_seq_failed_chain_without_risks_is_not_empty_queue(self):
        rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "review-since-tag"
        _write_guardian(
            work,
            risks=[],
            chain={
                "r_to_a": {"ok": True, "note": "n"},
                "a_to_t": {"ok": True, "note": "n"},
                "t_to_i": {"ok": False, "note": "n"},
                "i_to_r": {"ok": True, "note": "n"},
            },
        )
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--code-root",
                    ".",
                    "--test-command",
                    "true",
                    "apply",
                    "--seq",
                    "review-since-tag",
                ]
            )
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        plan = (work / "apply-plan.md").read_text(encoding="utf-8")
        self.assertNotIn("(none remaining)", plan)
        self.assertIn("t_to_i", plan)
        self.assertIn("apply: implementer", out)
        seq = _seq_state(work)
        self.assertTrue(seq["applied"], seq)
        _assert_no_related_apply(self, seq)
        self.assertTrue(
            any("implementer" in hop for step in seq["steps"] for hop in (step.get("hops") or [])),
            seq["steps"],
        )
        self.assertEqual(State.load(work).stop_reason, "applied")
        _assert_stop_applied_exhausted(self, work, seq)

    def test_seq_unknown_guardian_link_does_not_take_architecture_rail(self):
        rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "review-since-tag"
        _write_guardian(
            work,
            risks=[
                {
                    "title": "mystery hop",
                    "evidence": "bad link",
                    "path": "README",
                    "link": "t2i",
                    "kind": "implementation",
                }
            ],
            chain={
                "r_to_a": {"ok": True, "note": "n"},
                "a_to_t": {"ok": True, "note": "n"},
                "t_to_i": {"ok": True, "note": "n"},
                "i_to_r": {"ok": True, "note": "n"},
            },
        )
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--code-root",
                    ".",
                    "--test-command",
                    "true",
                    "apply",
                    "--seq",
                    "review-since-tag",
                ]
            )
        out = buf.getvalue()
        self.assertNotIn("architect replan", out)
        self.assertNotEqual(State.load(work).stop_reason, "applied")
        rows = collect_guardian_findings(work)
        self.assertTrue(rows)
        self.assertTrue(all(row["kind"] == "unclassified" for row in rows), rows)
        self.assertEqual(State.load(work).stop_reason, "needs-classification")

    def test_seq_reopen_skip_failed_restores_suffix(self):
        work = self._feature()
        self._inject_mixed(work)
        self.assertEqual(
            main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--test-command",
                    "true",
                    "apply",
                    "--seq",
                    "add-greet-helper",
                ]
            ),
            0,
        )
        data = json.loads((work / "findings.json").read_text(encoding="utf-8"))
        first = data["seq"]["steps"][0]["id"]
        second = data["seq"]["steps"][1]["id"]
        self.assertEqual(
            main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "apply",
                    "--seq",
                    "--reopen",
                    first,
                    "add-greet-helper",
                ]
            ),
            0,
        )
        self.assertEqual(
            main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--test-command",
                    "false",
                    "apply",
                    "--seq",
                    "add-greet-helper",
                ]
            ),
            1,
        )
        mid = _seq_state(work)
        self.assertEqual(mid["failed"], first)
        self.assertIn(second, mid["stale"])
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "--seq",
                "--skip-failed",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        seq = _seq_state(work)
        self.assertIn(first, seq["skipped"])
        self.assertNotIn(second, seq["stale"])
        self.assertIn(second, seq["applied"])
        hops = _last_applied_hops(seq, second)
        self.assertNotEqual(hops, ["related"])
        self.assertTrue(any("implementer" in hop for hop in hops), hops)
        follow = (work / "followups.md").read_text(encoding="utf-8")
        self.assertIn("**skipped**", follow)
        self.assertNotIn("**stale**", follow)
        self.assertEqual(State.load(work).stop_reason, "applied")
        _assert_stop_applied_exhausted(self, work, seq)

    def _arch_plus_related_t_to_i(self, work):
        arch = {
            "severity": "high",
            "title": "wrong module boundary",
            "evidence": "greet in the wrong package",
            "path": "src/greet.py",
            "kind": "architecture",
        }
        _inject_findings(work, [arch])
        _write_guardian(
            work,
            risks=[
                {
                    "title": "greet still accepts empty",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "link": "t_to_i",
                }
            ],
            chain={
                "r_to_a": {"ok": True, "note": "n"},
                "a_to_t": {"ok": True, "note": "n"},
                "t_to_i": {"ok": False, "note": "n"},
                "i_to_r": {"ok": True, "note": "n"},
            },
        )
        arch_id = finding_id(arch)
        related_id = finding_id(collect_guardian_findings(work)[0])
        return arch_id, related_id

    def test_seq_findings_prompt_is_exactly_current_item(self):
        work = self._feature()
        arch_id, related_id = self._arch_plus_related_t_to_i(work)
        related_title = collect_guardian_findings(work)[0]["title"]
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "false",
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 1)
        writer_phases = (
            "replan-questions",
            "replan",
            "tdd-design-apply",
            "test-writer-gate",
            "test-writer-apply",
            "implementer-apply",
        )
        for phase in writer_phases:
            path = work / "prompts" / ("%s.prompt.md" % phase)
            self.assertTrue(path.is_file(), phase)
            items = _parse_findings_block(path.read_text(encoding="utf-8"))
            self.assertIsNotNone(items, "Findings block missing in %s" % phase)
            self.assertEqual(len(items), 1, (phase, items))
            ids = {item.get("id") or finding_id(item) for item in items}
            self.assertEqual(ids, {arch_id}, phase)
            blob = json.dumps(items)
            self.assertNotIn(related_id, blob)
            self.assertNotIn("implementation", [item.get("kind") for item in items])
            self.assertNotIn(related_title, blob)

        finding_path = work / "seq" / arch_id / "finding.json"
        self.assertTrue(finding_path.is_file())
        dumped = json.loads(finding_path.read_text(encoding="utf-8"))
        rows = dumped if isinstance(dumped, list) else [dumped]
        self.assertEqual(len(rows), 1, dumped)
        self.assertEqual(rows[0].get("id") or finding_id(rows[0]), arch_id)
        self.assertNotIn(related_id, json.dumps(dumped))

    def test_seq_related_context_is_not_findings(self):
        work = self._feature()
        arch_id, related_id = self._arch_plus_related_t_to_i(work)
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "false",
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 1)
        related_path = work / "seq" / arch_id / "related.json"
        if related_path.is_file():
            related_blob = related_path.read_text(encoding="utf-8")
            self.assertIn(related_id, related_blob)
        for phase in ("replan-questions", "implementer-apply"):
            prompt = (work / "prompts" / ("%s.prompt.md" % phase)).read_text(encoding="utf-8")
            items = _parse_findings_block(prompt)
            self.assertEqual(len(items), 1)
            if "Related" in prompt or "related guardian" in prompt.lower():
                self.assertNotEqual(
                    _heading_for_id(prompt, related_id),
                    "Findings",
                    "related must not sit under the Findings heading",
                )
        seq = _seq_state(work)
        self.assertNotIn(related_id, seq.get("applied") or [])
        _assert_no_related_apply(self, seq)

    def test_seq_related_stays_queued_after_primary_applied(self):
        work = self._feature()
        arch_id, related_id = self._arch_plus_related_t_to_i(work)
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        seq = _seq_state(work)
        _assert_no_related_apply(self, seq)
        seen = []
        for step in seq.get("steps") or []:
            if step.get("status") == "applied":
                if step.get("id") == arch_id:
                    self.assertNotIn(related_id, seen)
                    self.assertNotIn(related_id, seq.get("skipped") or [])
                seen.append(step.get("id"))
        self.assertIn(related_id, seq.get("applied") or [])
        self.assertTrue(
            any("implementer" in hop for hop in _last_applied_hops(seq, related_id)),
            _last_applied_hops(seq, related_id),
        )
        self.assertNotEqual(_last_applied_hops(seq, related_id), ["related"])

    def test_apply_records_baseline_and_uses_configured_command(self):
        work = self._feature()
        stale = "python3 -m pytest -q inferedge-phase1/tests"
        state = json.loads((work / "state.json").read_text(encoding="utf-8"))
        state["test_command"] = stale
        (work / "state.json").write_text(json.dumps(state), encoding="utf-8")
        cfg = self.repo / ".team" / "config.toml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            '[paths]\ntest_command = "true"\ncode_root = "."\n',
            encoding="utf-8",
        )
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "apply",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        baseline = (work / "baseline-report.md").read_text(encoding="utf-8")
        report = (work / "apply-test-report.md").read_text(encoding="utf-8")
        self.assertIn("true", baseline)
        self.assertNotIn(stale, baseline)
        self.assertIn("true", report)
        self.assertNotIn(stale, report)
        self.assertIn("baseline", (work / "apply-summary.md").read_text(encoding="utf-8"))

    def test_apply_collection_death_does_not_invoke_debugger(self):
        work = self._feature()
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
        )
        collect = (
            "python3 -c \"print('ERROR collecting tests/unit/test_x.py\\n"
            "Interrupted: 1 errors during collection'); raise SystemExit(2)\""
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                collect,
                "apply",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        report = (work / "apply-test-report.md").read_text(encoding="utf-8")
        self.assertIn("UNVERIFIED", report)
        self.assertIn("collection", report.lower())
        dbg = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (work / "prompts").glob("*debugger*")
        )
        self.assertFalse(dbg.strip(), "collection death must not invoke debugger")

    def _collection_death_cmd(self):
        return (
            "python3 -c \"print('ERROR collecting tests/unit/test_x.py\\n"
            "Interrupted: 1 errors during collection'); raise SystemExit(2)\""
        )

    def _collection_death_with_assertionerror_cmd(self):
        return (
            "python3 -c \"print('ERROR collecting tests/conftest.py\\n"
            "AssertionError: conftest exploded during collection\\n"
            "Interrupted: 1 errors during collection'); raise SystemExit(2)\""
        )

    def test_apply_seq_timeout_does_not_mark_class_failed(self):
        work = self._feature()
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
        )
        old = os.environ.get("TEAM_PHASE_TIMEOUT")
        os.environ["TEAM_PHASE_TIMEOUT"] = "1"
        try:
            try:
                rc = main(
                    [
                        "--repo",
                        str(self.repo),
                        "--fake",
                        "--test-command",
                        "python3 -c 'import time; time.sleep(30)'",
                        "apply",
                        "--seq",
                        "add-greet-helper",
                    ]
                )
            except subprocess.TimeoutExpired as exc:
                self.fail("TimeoutExpired escaped apply --seq: %s" % exc)
        finally:
            if old is None:
                os.environ.pop("TEAM_PHASE_TIMEOUT", None)
            else:
                os.environ["TEAM_PHASE_TIMEOUT"] = old
        self.assertEqual(rc, 0)
        state = State.load(work)
        self.assertNotEqual(state.stop_reason, "seq-failed")
        self.assertNotEqual(state.stop_reason, "suite ERROR")
        seq = _seq_state(work)
        self.assertFalse(seq.get("failed"), seq)
        for step in seq.get("steps") or []:
            self.assertNotEqual(step.get("status"), "failed", step)
            self.assertNotEqual(step.get("suite"), "ERROR", step)

    def test_phase_baseline_timeout_records_unverified(self):
        cfg = load_config(
            self.repo,
            fake=True,
            force=True,
            test_command="true",
        )
        pipe = start_feature(cfg, "timeout baseline", "timeout-baseline")
        pipe.cfg.test_command = "python3 -c 'import time; time.sleep(30)'"
        pipe.cfg.phase_timeout = 1
        try:
            pipe.phase_baseline()
        except subprocess.TimeoutExpired as exc:
            self.fail("TimeoutExpired escaped phase_baseline: %s" % exc)
        self.assertEqual(pipe.state.baseline.get("status"), "UNVERIFIED")

    def test_team_review_slug_rewrites_followups_from_review_and_guardian(self):
        work = self._feature()
        (work / "followups.md").write_text("STALE-PRE-REVIEW\n", encoding="utf-8")
        review_out = {
            "summary": "ok",
            "findings": [
                {
                    "severity": "high",
                    "title": "slug-review-finding",
                    "evidence": "x",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
            "review_markdown": "ok",
        }
        guardian_out = {
            "risks": [
                {
                    "title": "slug-guardian-risk",
                    "evidence": "x",
                    "path": "src/greet.py",
                    "link": "i_to_r",
                }
            ],
            "guardian_markdown": "ok",
            "chain": {
                "r_to_a": {"ok": True, "note": "n"},
                "a_to_t": {"ok": True, "note": "n"},
                "t_to_i": {"ok": True, "note": "n"},
                "i_to_r": {"ok": True, "note": "n"},
            },
        }
        hostile = HostileRuntime(
            by_phase={
                "reviewer-fake": [emit(review_out)],
                "guardian": [emit(guardian_out)],
            },
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            rc = main(
                ["--repo", str(self.repo), "--fake", "review", "add-greet-helper"]
            )
        self.assertEqual(rc, 0)
        follow = (work / "followups.md").read_text(encoding="utf-8")
        self.assertNotIn("STALE-PRE-REVIEW", follow)
        self.assertIn("slug-review-finding", follow)
        self.assertIn("[implementation]", follow)
        self.assertIn("slug-guardian-risk", follow)
        self.assertIn("**high** [implementation] slug-review-finding", follow)

    def test_team_review_slug_writes_followups_when_guardian_skipped(self):
        work = self._feature()
        (work / "followups.md").write_text("STALE-PRE-REVIEW\n", encoding="utf-8")
        review_out = {
            "summary": "ok",
            "findings": [
                {
                    "severity": "medium",
                    "title": "slug-review-finding",
                    "evidence": "x",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
            "review_markdown": "ok",
        }
        hostile = HostileRuntime(
            by_phase={"reviewer-fake": [emit(review_out)]},
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            rc = main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--skip",
                    "guardian",
                    "review",
                    "add-greet-helper",
                ]
            )
        self.assertEqual(rc, 0)
        follow = (work / "followups.md").read_text(encoding="utf-8")
        self.assertNotIn("STALE-PRE-REVIEW", follow)
        self.assertIn("slug-review-finding", follow)
        self.assertIn("[implementation]", follow)

    def test_team_review_slug_does_not_skip_followups_when_apply_will_rewrite_later(self):
        work = self._feature()
        (work / "followups.md").write_text("STALE-PRE-REVIEW\n", encoding="utf-8")
        review_out = {
            "summary": "ok",
            "findings": [
                {
                    "severity": "high",
                    "title": "slug-review-finding",
                    "evidence": "x",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
            "review_markdown": "ok",
        }
        hostile = HostileRuntime(
            by_phase={"reviewer-fake": [emit(review_out)]},
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            rc = main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--skip",
                    "guardian",
                    "review",
                    "add-greet-helper",
                ]
            )
        self.assertEqual(rc, 0)
        follow = (work / "followups.md").read_text(encoding="utf-8")
        self.assertNotIn("STALE-PRE-REVIEW", follow)
        self.assertIn("slug-review-finding", follow)
        self.assertFalse(
            (work / "apply-summary.md").is_file(),
            "followups must already be current before apply",
        )

    def test_seq_collection_death_is_not_class_failure(self):
        work = self._feature()
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                self._collection_death_cmd(),
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        state = State.load(work)
        self.assertNotEqual(state.stop_reason, "seq-failed")
        self.assertEqual(state.stop_reason, "applied")
        seq = _seq_state(work)
        self.assertFalse(seq.get("failed"), seq)
        for step in seq.get("steps") or []:
            self.assertNotEqual(step.get("status"), "failed", step)
        dbg = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (work / "prompts").glob("*debugger*")
        )
        self.assertFalse(dbg.strip(), "UNVERIFIED must not invoke debugger")

    def test_seq_collection_death_with_assertionerror_is_not_class_failure(self):
        work = self._feature()
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                self._collection_death_with_assertionerror_cmd(),
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        state = State.load(work)
        self.assertNotEqual(state.stop_reason, "seq-failed")
        self.assertEqual(state.stop_reason, "applied")
        seq = _seq_state(work)
        self.assertFalse(seq.get("failed"), seq)
        for step in seq.get("steps") or []:
            self.assertNotEqual(step.get("status"), "failed", step)
        dbg = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (work / "prompts").glob("*debugger*")
        )
        self.assertFalse(dbg.strip(), "collection death must not invoke debugger")

    def test_seq_missing_command_is_not_class_failure(self):
        import shutil

        work = self._feature()
        shutil.rmtree(self.repo / "tests", ignore_errors=True)
        state = json.loads((work / "state.json").read_text(encoding="utf-8"))
        state["test_command"] = ""
        (work / "state.json").write_text(json.dumps(state), encoding="utf-8")
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertNotEqual(State.load(work).stop_reason, "seq-failed")
        self.assertEqual(State.load(work).stop_reason, "applied")
        seq = _seq_state(work)
        self.assertFalse(seq.get("failed"), seq)

    def test_seq_product_fail_still_fails_the_class(self):
        work = self._feature()
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "false",
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(State.load(work).stop_reason, "seq-failed")
        seq = _seq_state(work)
        self.assertTrue(seq.get("failed"), seq)

    def _product_fail_exit_5_cmd(self):
        """Non-pytest host command: exit 5, no collection language, no case names."""
        return (
            "python3 -c \"print('host suite product failure'); raise SystemExit(5)\""
        )

    def test_seq_exit_5_without_collection_language_is_class_failure(self):
        """Exit 5 with no collection text is a product FAIL; --seq must stop.

        collection_failed's exit 4/5 branch can mark this UNVERIFIED. UNVERIFIED
        is not a class failure, so --seq would apply. The converse of collection
        death must be evaluated: no collection language, empty parse_failing_names,
        status FAIL, stop_reason seq-failed.
        """
        work = self._feature()
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                }
            ],
        )
        cmd = self._product_fail_exit_5_cmd()
        self.assertNotIn("pytest", cmd)
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                cmd,
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(State.load(work).stop_reason, "seq-failed")
        self.assertNotEqual(State.load(work).stop_reason, "applied")
        seq = _seq_state(work)
        self.assertTrue(seq.get("failed"), seq)
        report = (work / "apply-test-report.md").read_text(encoding="utf-8")
        self.assertIn("FAIL", report)
        self.assertNotIn("verdict: **UNVERIFIED**", report)

    def test_seq_unverified_does_not_block_later_classes(self):
        work = self._feature()
        self._inject_mixed(work)
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                self._collection_death_cmd(),
                "apply",
                "--seq",
                "add-greet-helper",
            ]
        )
        self.assertNotEqual(State.load(work).stop_reason, "seq-failed")
        seq = _seq_state(work)
        self.assertFalse(seq.get("failed"), seq)
        kinds = [step.get("kind") for step in seq.get("steps") or []]
        self.assertIn("architecture", kinds)
        self.assertIn("implementation", kinds)
        self.assertEqual(rc, 0)

    def test_apply_report_final_fail_not_unverified_when_baseline_unverified(self):
        from unittest import mock

        from team.config import load_config
        from team.pipeline import start_feature

        cfg = load_config(
            self.repo,
            fake=True,
            force=True,
            code_root="src",
            test_root="tests",
            test_command="true",
        )
        pipe = start_feature(cfg, "brief", "cmp-unverified-fail")
        pipe.state.baseline = {
            "status": "UNVERIFIED",
            "failing": [],
            "command": "",
            "exit": None,
            "output": "(no test command discovered)",
        }
        fail_run = {
            "command": "false",
            "exit": 1,
            "status": "FAIL",
            "output": "FAILED tests/a.py::test_x\n",
            "failing": ["tests/a.py::test_x"],
            "names_unparsed": False,
            "collection_failed": False,
        }
        with mock.patch("team.pipeline.testhost.run_suite", return_value=fail_run):
            run = pipe._run_apply_suite()
        cmp = run.get("comparison") or {}
        self.assertEqual(run.get("status"), "FAIL")
        self.assertEqual(cmp.get("final_status"), "FAIL")
        self.assertNotEqual(cmp.get("verdict"), "UNVERIFIED")
        report = (pipe.work / "apply-test-report.md").read_text(encoding="utf-8")
        self.assertIn("FAIL", report)
        self.assertNotIn("verdict: **UNVERIFIED**", report)

    def test_range_apply_writes_worktree_without_review(self):
        rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "review-since-tag"
        review_before = (work / "review.md").read_text(encoding="utf-8")
        guardian_before = (work / "guardian.md").read_text(encoding="utf-8")
        last = State.load(work).last_review or {}
        attempt_before = last.get("attempt")
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "readme typo as stand-in bug",
                    "evidence": "README",
                    "path": "README",
                    "kind": "implementation",
                }
            ],
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--code-root",
                ".",
                "--test-command",
                "true",
                "apply",
                "review-since-tag",
            ]
        )
        self.assertEqual(rc, 0)
        patch = (work / "git" / "apply.patch").read_text(encoding="utf-8")
        names = (work / "git" / "apply-names.txt").read_text(encoding="utf-8")
        self.assertNotIn("(empty apply tree)", patch)
        self.assertIn("greet.py", patch + names)
        range_md = (work / "range.md").read_text(encoding="utf-8")
        self.assertIn("Apply working tree", range_md)
        self.assertEqual((work / "review.md").read_text(encoding="utf-8"), review_before)
        self.assertEqual((work / "guardian.md").read_text(encoding="utf-8"), guardian_before)
        self.assertEqual((State.load(work).last_review or {}).get("attempt"), attempt_before)
        summary = (work / "apply-summary.md").read_text(encoding="utf-8")
        self.assertNotIn("Closing review", summary)
        self.assertNotIn("closing review", summary.lower())
        apply_prompts = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (work / "prompts").glob("*reviewer*")
        )
        self.assertNotIn("CLOSING APPLY REVIEW", apply_prompts)
        rc = main(["--repo", str(self.repo), "--fake", "review", "review-since-tag"])
        self.assertEqual(rc, 0)
        prompts = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (work / "prompts").glob("*reviewer*")
        )
        self.assertIn("git/apply.patch", prompts)
        self.assertNotIn("CLOSING APPLY REVIEW", prompts)

    def test_apply_without_slug_uses_review_since_tag(self):
        rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "review-since-tag"
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "readme typo as stand-in bug",
                    "evidence": "README",
                    "path": "README",
                    "kind": "implementation",
                }
            ],
        )
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--code-root",
                ".",
                "--test-command",
                "true",
                "apply",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue((work / "apply-impl-summary.md").is_file())
        self.assertEqual(State.load(work).stop_reason, "applied")

    def test_apply_without_slug_needs_default_work(self):
        rc = main(["--repo", str(self.repo), "--fake", "apply"])
        self.assertEqual(rc, 1)

    def test_apply_test_writer_consults_implementer(self):
        from tests.support.hostile import HostileRuntime, emit, register_runtime, write

        work = self._feature()
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "nested test_root is a vacuous census",
                    "evidence": "status != UNVERIFIED",
                    "path": "tests/test_testhost.py",
                    "kind": "test",
                }
            ],
        )
        hostile = HostileRuntime(
            by_phase={
                "test-writer-gate": [
                    emit(
                        {
                            "ready": False,
                            "consult": "implementer",
                            "questions": [
                                "Will discover_test_command stay empty when test_root is nested?"
                            ],
                            "summary": "need production shape",
                        }
                    )
                ],
                "test-writer-apply": [
                    write(
                        "tests/test_nested.py",
                        "def test_nested():\n    assert True\n",
                    ),
                    emit(
                        {
                            "summary": "added nested test",
                            "paths_touched": ["tests/test_nested.py"],
                        }
                    ),
                ],
            }
        )
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
        consults = list((work / "consult").glob("*test-writer-implementer.json"))
        self.assertTrue(consults, "test-writer must consult implementer")
        asked = json.loads(consults[0].read_text(encoding="utf-8"))
        self.assertEqual(asked.get("to"), "implementer")
        self.assertTrue((self.repo / "tests" / "test_nested.py").is_file())
        self.assertFalse((work / "apply-impl-summary.md").is_file())

    def test_apply_test_writer_does_not_see_implementation_findings(self):
        work = self._feature()
        _inject_findings(
            work,
            [
                {
                    "severity": "high",
                    "title": "greet ignores empty name",
                    "evidence": "no guard",
                    "path": "src/greet.py",
                    "kind": "implementation",
                },
                {
                    "severity": "high",
                    "title": "nested test_root is a vacuous census",
                    "evidence": "status != UNVERIFIED",
                    "path": "tests/test_testhost.py",
                    "kind": "test",
                },
            ],
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
        self.assertEqual(rc, 0)
        writer = (work / "prompts" / "test-writer-apply.prompt.md").read_text(
            encoding="utf-8"
        )
        impl = (work / "prompts" / "implementer-apply.prompt.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("nested test_root is a vacuous census", writer)
        self.assertNotIn("greet ignores empty name", writer)
        self.assertIn("greet ignores empty name", impl)
        self.assertNotIn("nested test_root is a vacuous census", impl)


def _parse_findings_block(text: str):
    marker = None
    for key in ("Findings:\n", "Findings:"):
        idx = text.find(key)
        if idx >= 0:
            marker = idx + len(key)
            break
    if marker is None:
        return None
    rest = text[marker:].lstrip()
    try:
        obj, _end = json.JSONDecoder().raw_decode(rest)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict):
        return [obj]
    if isinstance(obj, list):
        return obj
    return None


def _heading_for_id(text: str, fid: str) -> str:
    pos = text.find(fid)
    if pos < 0:
        return ""
    head = text[:pos]
    labels = []
    for label in ("Findings", "Related", "Class", "Related guardian"):
        labels.extend((head.rfind(label), label) for _ in [0] if label in head)
    labels = [(i, name) for i, name in labels if i >= 0]
    if not labels:
        return ""
    return max(labels)[1]


if __name__ == "__main__":
    unittest.main()
