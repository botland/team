import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.cli import main
from team.gitutil import last_dedicated_tag, resolve_review_base, stamp_reviewed
from team.state import State


def _git(repo: Path, *args: str) -> None:
    subprocess.check_call(["git", "-C", str(repo), *args], stdout=subprocess.DEVNULL)


def _git_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "README").write_text("one\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-m", "first")


class RangeReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _git_repo(self.repo)
        os.environ["TEAM_HOME"] = str(Path(__file__).resolve().parents[1])

    def tearDown(self):
        self.tmp.cleanup()

    def test_prefers_reviewed_tag(self):
        _git(self.repo, "tag", "v0.1")
        _git(self.repo, "tag", "reviewed-20200101-0000")
        (self.repo / "README").write_text("two\n", encoding="utf-8")
        _git(self.repo, "add", "README")
        _git(self.repo, "commit", "-m", "second")
        self.assertEqual(last_dedicated_tag(self.repo), "reviewed-20200101-0000")
        base, kind = resolve_review_base(self.repo)
        self.assertEqual(kind, "dedicated")
        self.assertEqual(base, "reviewed-20200101-0000")

    def test_review_since_dedicated_tag(self):
        _git(self.repo, "tag", "reviewed-20200101-0000")
        (self.repo / "README").write_text("two\n", encoding="utf-8")
        _git(self.repo, "add", "README")
        _git(self.repo, "commit", "-m", "second")
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "review",
                "--force",
            ]
        )
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "review-since-tag"
        state = State.load(work)
        self.assertEqual(state.mode, "range")
        self.assertEqual(state.range_kind, "dedicated")
        brief = (work / "brief.md").read_text(encoding="utf-8")
        self.assertIn("reviewed-20200101-0000", brief)
        log = (work / "git" / "log.txt").read_text(encoding="utf-8")
        self.assertIn("second", log)
        self.assertTrue((work / "review.md").is_file())
        self.assertTrue((work / "git" / "diff.patch").is_file())

    def test_review_whole_branch_without_tags(self):
        rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        state = State.load(self.repo / ".team" / "work" / "review-since-tag")
        self.assertEqual(state.range_kind, "branch")

    def test_stamp_creates_reviewed_tag(self):
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "review",
                "--stamp",
                "--force",
            ]
        )
        self.assertEqual(rc, 0)
        state = State.load(self.repo / ".team" / "work" / "review-since-tag")
        self.assertTrue(state.stamp_tag.startswith("reviewed-"))
        self.assertEqual(last_dedicated_tag(self.repo), state.stamp_tag)

    def test_stamp_helper(self):
        tag = stamp_reviewed(self.repo)
        self.assertTrue(tag.startswith("reviewed-"))
        self.assertEqual(last_dedicated_tag(self.repo), tag)


if __name__ == "__main__":
    unittest.main()
