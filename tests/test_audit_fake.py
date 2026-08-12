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


class FakeAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _git_repo(self.repo)
        os.environ["TEAM_HOME"] = str(Path(__file__).resolve().parents[1])

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_audit(self):
        rc = main(["--repo", str(self.repo), "--fake", "audit", "--force"])
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "audit"
        for name in (
            "brief.md",
            "scout.md",
            "scout.json",
            "status.md",
            "review.md",
            "report.md",
            "state.json",
        ):
            self.assertTrue((work / name).is_file(), name)
        state = State.load(work)
        self.assertEqual(state.mode, "audit")
        self.assertEqual(state.stop_reason, "complete")
        self.assertEqual(state.phases_done, ["scout", "assess", "reviewer"])
        report = (work / "report.md").read_text(encoding="utf-8")
        self.assertIn("# Status", report)
        self.assertIn("# Review", report)
        # Read-only: do not create production/test trees
        self.assertFalse((self.repo / "src").exists())
        self.assertFalse((self.repo / "tests").exists())

    def test_leftover_repo_and_query(self):
        rc = main(
            [
                "--fake",
                "audit",
                "--force",
                str(self.repo),
                "what's missing",
            ]
        )
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "audit-what-s-missing"
        self.assertTrue((work / "report.md").is_file())
        brief = (work / "brief.md").read_text(encoding="utf-8")
        self.assertIn("missing", brief)

    def test_non_git_repo_allowed(self):
        bare = Path(self.tmp.name) / "bare"
        bare.mkdir()
        (bare / "README").write_text("x\n", encoding="utf-8")
        rc = main(["--repo", str(bare), "--fake", "audit", "--force"])
        self.assertEqual(rc, 0)
        self.assertTrue((bare / ".team" / "work" / "audit" / "report.md").is_file())

    def test_dry_run_stops_before_review(self):
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "audit",
                "--dry-run",
                "--force",
            ]
        )
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "audit"
        state = State.load(work)
        self.assertEqual(state.stop_reason, "dry_run")
        self.assertEqual(state.phases_done, ["scout", "assess"])
        self.assertTrue((work / "status.md").is_file())
        self.assertTrue((work / "report.md").is_file())
        self.assertFalse((work / "review.md").is_file())


if __name__ == "__main__":
    unittest.main()
