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


def _write_guardian(work: Path, *, risks, chain) -> None:
    (work / "prompts").mkdir(parents=True, exist_ok=True)
    (work / "prompts" / "guardian.result.json").write_text(
        json.dumps(
            {
                "risks": risks,
                "guardian_markdown": "injected",
                "chain": chain,
            }
        ),
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
                    "--no-review",
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
        self.assertIn("Re-reviewed to classify: no", plan)
        self.assertNotIn("Unchanged assumptions", (work / "design.md").read_text(encoding="utf-8"))
        self.assertEqual((work / "design.md").read_text(encoding="utf-8"), design_before)
        state = State.load(work)
        self.assertEqual(state.stop_reason, "applied")

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
                "--no-review",
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
                "--no-review",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue((work / "design-replan.md").is_file())
        design = (work / "design.md").read_text(encoding="utf-8")
        self.assertIn("Unchanged assumptions", design)
        summary = (work / "apply-summary.md").read_text(encoding="utf-8")
        self.assertIn("architect replan", summary)
        self.assertIn("implementer", summary)
        self.assertIn("test-writer", summary)

    def test_apply_unclassified_rereviews(self):
        work = self._feature()
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
        self.assertIn("Re-reviewed to classify: yes", plan)
        # Fake re-review emits no findings, so nothing to apply.
        summary = (work / "apply-summary.md").read_text(encoding="utf-8")
        self.assertIn("implementation=0", summary)

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

    def test_seq_applies_guardian_when_review_is_notes_only(self):
        rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "review-since-tag"
        (work / "prompts" / "guardian.result.json").write_text(
            json.dumps(
                {
                    "risks": [
                        {
                            "title": "slash still taught as legal",
                            "evidence": "error names slash",
                            "path": "README",
                            "link": "t_to_i",
                        }
                    ],
                    "guardian_markdown": "one risk",
                    "chain": {
                        "r_to_a": {"ok": True, "note": "n"},
                        "a_to_t": {"ok": True, "note": "n"},
                        "t_to_i": {"ok": False, "note": "n"},
                        "i_to_r": {"ok": True, "note": "n"},
                    },
                }
            ),
            encoding="utf-8",
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
                    "--no-review",
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
                "--no-review",
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
        seq_dirs = list((work / "seq").iterdir())
        self.assertEqual(len(seq_dirs), 2)
        reviews = list((work / "seq").glob("*/review.md"))
        self.assertEqual(len(reviews), 2)
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
                "--no-review",
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
                "--no-review",
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
                    "--no-review",
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
                "--no-review",
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
                    "--no-review",
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
                "--no-review",
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
                    "--no-review",
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
                "--no-review",
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
                "--no-review",
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
                "--no-review",
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
                "--no-review",
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
                    "--no-review",
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
                    "--no-review",
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
                    "--no-review",
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
                    "--no-review",
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
                "--no-review",
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


if __name__ == "__main__":
    unittest.main()
