import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


class FakePipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _git_repo(self.repo)
        os.environ["TEAM_HOME"] = str(Path(__file__).resolve().parents[1])

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv):
        return main(argv)

    def test_full_fake_feature(self):
        rc = self._run(
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
        for name in (
            "brief.md",
            "design.md",
            "critic.md",
            "test-contract.md",
            "tdd-summary.md",
            "impl-summary.md",
            "baseline-report.md",
            "test-report.md",
            "review.md",
            "guardian.md",
            "adversarial.md",
            "state.json",
        ):
            self.assertTrue((work / name).is_file(), name)
        self.assertTrue((self.repo / "tests" / "test_greet.py").is_file())
        self.assertTrue((self.repo / "src" / "greet.py").is_file())
        state = State.load(work)
        self.assertEqual(state.stop_reason, "complete")
        self.assertIn("architect", state.phases_done)
        self.assertIn("implementer", state.phases_done)
        self.assertIn("reviewer", state.phases_done)
        review = (work / "review.md").read_text(encoding="utf-8")
        self.assertIn("# Review", review)

    def test_dry_run_writes_no_tests(self):
        rc = self._run(
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
        self.assertFalse((self.repo / "tests" / "test_greet.py").exists())
        self.assertFalse((self.repo / "src" / "greet.py").exists())
        work = self.repo / ".team" / "work" / "add-greet-helper"
        self.assertTrue((work / "design.md").is_file())
        self.assertTrue((work / "test-contract.md").is_file())
        state = State.load(work)
        self.assertEqual(state.stop_reason, "dry_run")
        prompt = (work / "prompts" / "architect.prompt.md").read_text(encoding="utf-8")
        self.assertIn("Enumerate the space", prompt)
        self.assertIn("Close the class", prompt)

    def test_resume_after_dry_run(self):
        self._run(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "feature",
                "--dry-run",
                "Add greet helper",
            ]
        )
        rc = self._run(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "resume",
                "add-greet-helper",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue((self.repo / "src" / "greet.py").is_file())
        state = State.load(self.repo / ".team" / "work" / "add-greet-helper")
        self.assertEqual(state.stop_reason, "complete")

    def test_replan(self):
        self._run(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--test-command",
                "true",
                "feature",
                "Add greet helper",
            ]
        )
        rc = self._run(["--repo", str(self.repo), "--fake", "replan", "add-greet-helper"])
        self.assertEqual(rc, 0)
        text = (
            self.repo / ".team" / "work" / "add-greet-helper" / "design-replan.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Unchanged assumptions", text)

    def test_status_and_roles(self):
        self._run(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "feature",
                "--dry-run",
                "Add greet helper",
            ]
        )
        self.assertEqual(self._run(["--repo", str(self.repo), "status", "add-greet-helper"]), 0)
        self.assertEqual(self._run(["--repo", str(self.repo), "roles"]), 0)

    def test_refuse_without_force(self):
        argv = [
            "--repo",
            str(self.repo),
            "--fake",
            "feature",
            "--dry-run",
            "Add greet helper",
        ]
        self.assertEqual(self._run(argv), 0)
        self.assertEqual(self._run(argv), 1)


if __name__ == "__main__":
    unittest.main()
