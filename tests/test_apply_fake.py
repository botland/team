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
from team.state import State


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


if __name__ == "__main__":
    unittest.main()
