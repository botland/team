import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.cli import main
from team.config import load_config
from team.pipeline import PipelineError, start_audit
from team.state import State
from tests.support.hostile import HostileRuntime, emit, register_runtime, write


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
        self.assertFalse((bare / "src").exists())
        self.assertFalse((bare / "tests").exists())

    def test_non_git_audit_product_write_is_fence_error_and_does_not_persist(self):
        """report.md existing is not the fence. Product bytes must not persist."""
        bare = Path(self.tmp.name) / "bare-hostile"
        bare.mkdir()
        (bare / "README").write_text("x\n", encoding="utf-8")
        cfg = load_config(bare, fake=True, force=True)
        pipe = start_audit(cfg, "what's missing", "audit")
        hostile = HostileRuntime(
            [
                write("src/pwned.py", "pwned-audit\n"),
                emit(
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
                        "notes": "hostile",
                    }
                ),
            ],
            phases=("scout",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_scout()
        self.assertIn("src/pwned.py", str(ctx.exception))
        self.assertFalse((bare / "src" / "pwned.py").exists())
        self.assertFalse(
            (bare / "src").exists() and any((bare / "src").iterdir()),
            "audit must not leave product bytes outside .team/work/",
        )

    def test_audit_write_outside_repo_is_fence_error_and_does_not_persist(self):
        outside = Path(self.tmp.name) / "vibe.rc"
        outside.write_text("user-outside\n", encoding="utf-8")
        cfg = load_config(self.repo, fake=True, force=True)
        pipe = start_audit(cfg, "status?", "audit-out")
        hostile = HostileRuntime(
            [
                write(str(outside), "pwned-audit\n"),
                emit(
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
                        "notes": "hostile",
                    }
                ),
            ],
            phases=("scout",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_scout()
        self.assertTrue(
            "vibe.rc" in str(ctx.exception) or str(outside) in str(ctx.exception),
            ctx.exception,
        )
        self.assertEqual(outside.read_text(encoding="utf-8"), "user-outside\n")
        self.assertFalse((self.repo / "src").exists())

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
