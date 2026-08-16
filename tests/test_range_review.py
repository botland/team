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
from team.gitutil import (
    last_any_tag,
    last_dedicated_tag,
    resolve_review_base,
    stamp_reviewed,
)
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

    def test_both_watermark_fallbacks_order_tags_the_same_way(self):
        """One rule for "the last tag", whichever pattern asks for it.

        --mark <ref> stamps an arbitrary ref, so a tag created today at an
        old commit is a deliberate rewind. describe --tags picked the
        topologically nearest tag instead and silently ignored it, so the
        two fallbacks chose different bases for the same history.
        """
        for text in ("two", "three"):
            (self.repo / "README").write_text(text + "\n", encoding="utf-8")
            _git(self.repo, "add", "README")
            _git(self.repo, "commit", "-m", text)
        old = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD~2"], text=True
        ).strip()
        # Newest tag by topology is at HEAD~1; the rewind is stamped later.
        _git(self.repo, "tag", "v0.2", "HEAD~1")
        _git(self.repo, "tag", "v0.1-rewind", old)
        self.assertEqual(last_any_tag(self.repo), "v0.1-rewind")
        base, kind = resolve_review_base(self.repo)
        self.assertEqual((base, kind), ("v0.1-rewind", "tag"))
        _git(self.repo, "tag", "reviewed-20200102-0000", "HEAD~1")
        _git(self.repo, "tag", "reviewed-20200101-0000", old)
        self.assertEqual(last_dedicated_tag(self.repo), "reviewed-20200101-0000")
        self.assertEqual(
            resolve_review_base(self.repo), ("reviewed-20200101-0000", "dedicated")
        )

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
        self.assertTrue((work / "census.md").is_file())
        self.assertTrue((work / "git" / "diff.patch").is_file())

    def test_review_whole_branch_without_tags(self):
        err = StringIO()
        with mock.patch("sys.stderr", err):
            rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "review-since-tag"
        state = State.load(work)
        self.assertEqual(state.range_kind, "branch")
        warning = err.getvalue()
        self.assertIn("no reviewed-* tag", warning)
        self.assertIn("whole branch from empty tree", warning)
        self.assertIn("team review --mark HEAD", warning)
        reviewer = (work / "prompts" / "reviewer-fake.prompt.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("git/names.txt", reviewer)
        self.assertIn("commit set", reviewer)
        self.assertIn("live dirty working tree", reviewer)
        self.assertIn("git/diff.patch", reviewer)
        self.assertIn("git/apply.patch", reviewer)
        self.assertNotIn("Dirty AGENTS.md is not R", reviewer)
        self.assertNotIn("too large to slurp", reviewer)
        self.assertNotIn("Already inventoried", reviewer)
        guardian = (work / "prompts" / "guardian.prompt.md").read_text(encoding="utf-8")
        self.assertIn("git/names.txt", guardian)
        self.assertIn("live dirty working tree", guardian)
        self.assertIn("Do not restate", guardian)
        self.assertIn("Do not recensus", guardian)
        self.assertIn("git/diff.patch", guardian.split("Work directory:", 1)[-1])
        self.assertIn("census.md", guardian)
        self.assertIn("review.md", guardian)

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

    def test_stamp_at_older_commit(self):
        (self.repo / "README").write_text("two\n", encoding="utf-8")
        _git(self.repo, "add", "README")
        _git(self.repo, "commit", "-m", "second")
        tag = stamp_reviewed(self.repo, "HEAD~1")
        base, kind = resolve_review_base(self.repo)
        self.assertEqual(kind, "dedicated")
        self.assertEqual(base, tag)
        from team.gitutil import commit_count

        self.assertEqual(commit_count(self.repo, tag), 1)

    def test_guardian_session_limit_skips_and_keeps_review(self):
        import json

        from tests.support.hostile import HostileRuntime, crash, register_runtime

        payload = json.dumps(
            {
                "is_error": True,
                "api_error_status": 429,
                "result": "You've hit your session limit · resets 4:30pm (UTC)",
            }
        )
        hostile = HostileRuntime([crash(1, payload)], phases=("guardian",))
        with register_runtime("fake", hostile):
            rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        work = self.repo / ".team" / "work" / "review-since-tag"
        self.assertTrue((work / "review.md").is_file())
        state = State.load(work)
        self.assertEqual(state.stop_reason, "complete")
        self.assertIn("guardian", state.skipped)

    def test_past_commits_uses_range_reviewer_both_from_config(self):
        team = self.repo / ".team"
        team.mkdir()
        (team / "config.toml").write_text(
            '[review]\nrange_reviewer = "both"\n', encoding="utf-8"
        )
        rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        state = State.load(self.repo / ".team" / "work" / "review-since-tag")
        self.assertEqual(state.assignment["reviewer"], "both")

    def test_past_commits_uses_one_grok_reviewer(self):
        rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        state = State.load(self.repo / ".team" / "work" / "review-since-tag")
        self.assertEqual(state.assignment["reviewer"], "grok")
        self.assertTrue((self.repo / ".team" / "work" / "review-since-tag" / "guardian.md").is_file())

    def test_pr_uses_both_reviewers(self):
        rc = main(["--repo", str(self.repo), "--fake", "review", "--pr", "12", "--force", "--no-stamp"])
        self.assertEqual(rc, 0)
        state = State.load(self.repo / ".team" / "work" / "review-pr-12")
        self.assertEqual(state.mode, "range")
        self.assertEqual(state.range_kind, "pr")
        self.assertEqual(state.assignment["reviewer"], "both")

    def test_pr_stays_both_when_config_reviewer_is_single(self):
        team = self.repo / ".team"
        team.mkdir()
        (team / "config.toml").write_text('[roles]\nreviewer = "claude"\n', encoding="utf-8")
        rc = main(["--repo", str(self.repo), "--fake", "review", "--pr", "3", "--force", "--no-stamp"])
        self.assertEqual(rc, 0)
        state = State.load(self.repo / ".team" / "work" / "review-pr-3")
        self.assertEqual(state.assignment["reviewer"], "both")

    def test_past_commits_accepts_both(self):
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--assign",
                "reviewer=both",
                "review",
                "--force",
            ]
        )
        self.assertEqual(rc, 0)
        state = State.load(self.repo / ".team" / "work" / "review-since-tag")
        self.assertEqual(state.assignment["reviewer"], "both")

    def test_past_commits_reviewer_flag_both(self):
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "review",
                "--reviewer",
                "both",
                "--force",
            ]
        )
        self.assertEqual(rc, 0)
        state = State.load(self.repo / ".team" / "work" / "review-since-tag")
        self.assertEqual(state.assignment["reviewer"], "both")

    def test_past_commits_reviewer_flag_claude(self):
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "review",
                "--reviewer",
                "claude",
                "--force",
            ]
        )
        self.assertEqual(rc, 0)
        state = State.load(self.repo / ".team" / "work" / "review-since-tag")
        self.assertEqual(state.assignment["reviewer"], "claude")

    def test_past_commits_assign_claude(self):
        rc = main(
            [
                "--repo",
                str(self.repo),
                "--fake",
                "--assign",
                "reviewer=claude",
                "review",
                "--force",
            ]
        )
        self.assertEqual(rc, 0)
        state = State.load(self.repo / ".team" / "work" / "review-since-tag")
        self.assertEqual(state.assignment["reviewer"], "claude")

    def test_list_mark_delete_tags(self):
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = main(["--repo", str(self.repo), "review", "--list-tags"])
        self.assertEqual(rc, 0)
        self.assertIn("no reviewed-* tags", buf.getvalue())

        rc = main(["--repo", str(self.repo), "review", "--mark"])
        self.assertEqual(rc, 0)
        self.assertTrue(last_dedicated_tag(self.repo).startswith("reviewed-"))

        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = main(["--repo", str(self.repo), "review", "--list-tags"])
        self.assertEqual(rc, 0)
        self.assertIn("next-base", buf.getvalue())
        self.assertIn("reviewed-", buf.getvalue())

        (self.repo / "README").write_text("two\n", encoding="utf-8")
        _git(self.repo, "add", "README")
        _git(self.repo, "commit", "-m", "second")

        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = main(["--repo", str(self.repo), "review", "--show-range"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("kind: dedicated", out)
        self.assertIn("commits: 1", out)
        self.assertIn("reviewer: grok", out)

        tag = last_dedicated_tag(self.repo)
        rc = main(["--repo", str(self.repo), "review", "--delete-tag", tag])
        self.assertEqual(rc, 0)
        self.assertEqual(last_dedicated_tag(self.repo), "")

    def test_mark_sets_next_review_base(self):
        rc = main(["--repo", str(self.repo), "review", "--mark", "HEAD"])
        self.assertEqual(rc, 0)
        (self.repo / "README").write_text("two\n", encoding="utf-8")
        _git(self.repo, "add", "README")
        _git(self.repo, "commit", "-m", "second")
        rc = main(["--repo", str(self.repo), "--fake", "review", "--force"])
        self.assertEqual(rc, 0)
        state = State.load(self.repo / ".team" / "work" / "review-since-tag")
        self.assertEqual(state.range_kind, "dedicated")
        self.assertTrue(state.range_base.startswith("reviewed-"))
        log = (
            self.repo / ".team" / "work" / "review-since-tag" / "git" / "log.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("second", log)

    def test_help_on_each_command(self):
        from team.cli import _parser

        parser = _parser()
        commands = [[]]
        for action in parser._actions:
            if getattr(action, "choices", None) and action.dest == "cmd":
                commands.extend([[name] for name in sorted(action.choices)])
                break
        self.assertIn(["costs"], commands)
        for argv in commands:
            buf = StringIO()
            err = StringIO()
            with mock.patch("sys.stdout", buf), mock.patch("sys.stderr", err):
                with self.assertRaises(SystemExit) as ctx:
                    main(argv + ["--help"])
            self.assertEqual(ctx.exception.code, 0, argv)
            text = buf.getvalue() + err.getvalue()
            self.assertIn("usage:", text)
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as ctx:
                main(["review", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        help_text = buf.getvalue()
        self.assertIn("--reviewer", help_text)
        self.assertIn("{claude,grok,both}", help_text)
        self.assertNotIn("rejected on past-commits", help_text)

    def test_delete_refuses_non_reviewed_tag(self):
        _git(self.repo, "tag", "v0.1")
        rc = main(["--repo", str(self.repo), "review", "--delete-tag", "v0.1"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
