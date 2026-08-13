"""A reviewed-* tag is evidence of a completed review, not a bare watermark."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.cli import main
from team.gitutil import last_dedicated_tag, list_reviewed_tags
from tests.support.hostile import HostileRuntime, emit, register_runtime
from tests.support.repo import git, head_sha, init_repo


def _tag_type(repo: Path, tag: str) -> str:
    return git(repo, "cat-file", "-t", tag).strip()


def _tag_payload(repo: Path, tag: str) -> str:
    return git(repo, "cat-file", "-p", tag)


class StampContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def _install_gh(self, body: str) -> Path:
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir(exist_ok=True)
        gh = bindir / "gh"
        gh.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        return bindir

    def _path_with(self, bindir: Path) -> str:
        return str(bindir) + ":/usr/bin:/bin"

    def _path_without_gh(self) -> str:
        bindir = Path(self.tmp.name) / "nogh"
        bindir.mkdir(exist_ok=True)
        return str(bindir) + ":/usr/bin:/bin"

    def test_successful_stamp_is_annotated_at_head_with_evidence(self):
        head = head_sha(self.repo)
        rc = main(
            ["--repo", str(self.repo), "--fake", "review", "--stamp", "--force"]
        )
        self.assertEqual(rc, 0)
        tag = last_dedicated_tag(self.repo)
        self.assertTrue(tag.startswith("reviewed-"))
        self.assertEqual(_tag_type(self.repo, tag), "tag")
        payload = _tag_payload(self.repo, tag)
        self.assertIn("review-since-tag", payload)
        self.assertIn(head, payload)
        pointed = git(self.repo, "rev-list", "-n", "1", tag).strip()
        self.assertEqual(pointed, head)

    def test_stamp_after_clean_run_is_the_next_base_with_zero_pending(self):
        rc = main(
            ["--repo", str(self.repo), "--fake", "review", "--stamp", "--force"]
        )
        self.assertEqual(rc, 0)
        from team.gitutil import commit_count, resolve_review_base

        base, kind = resolve_review_base(self.repo)
        self.assertEqual(kind, "dedicated")
        self.assertEqual(base, last_dedicated_tag(self.repo))
        self.assertEqual(commit_count(self.repo, base), 0)

    def test_skipped_guardian_warns_and_stamps(self):
        buf = StringIO()
        with mock.patch("sys.stderr", buf):
            rc = main(
                [
                    "--repo",
                    str(self.repo),
                    "--fake",
                    "--skip",
                    "guardian",
                    "review",
                    "--stamp",
                    "--force",
                ]
            )
        self.assertEqual(rc, 0)
        tag = last_dedicated_tag(self.repo)
        self.assertTrue(tag)
        self.assertEqual(_tag_type(self.repo, tag), "tag")
        payload = _tag_payload(self.repo, tag)
        self.assertIn("guardian=skipped", payload)
        err = buf.getvalue().lower()
        self.assertTrue("warn" in err or "skip" in err, err)

    def test_dirty_tree_warns_and_stamps(self):
        (self.repo / "README").write_text("dirty\n", encoding="utf-8")
        buf = StringIO()
        with mock.patch("sys.stderr", buf):
            rc = main(
                ["--repo", str(self.repo), "--fake", "review", "--stamp", "--force"]
            )
        self.assertEqual(rc, 0)
        self.assertTrue(last_dedicated_tag(self.repo).startswith("reviewed-"))
        err = buf.getvalue()
        self.assertTrue(err.strip(), "dirty tree must warn on stamp")

    def test_high_severity_findings_still_allow_a_stamp(self):
        hostile = HostileRuntime(
            [
                emit(
                    {
                        "summary": "blocked?",
                        "findings": [
                            {
                                "severity": "high",
                                "title": "bug",
                                "evidence": "x",
                                "path": "README",
                                "kind": "implementation",
                            }
                        ],
                    }
                )
            ],
            phases=("reviewer-fake",),
        )
        with register_runtime("fake", hostile):
            rc = main(
                ["--repo", str(self.repo), "--fake", "review", "--stamp", "--force"]
            )
        self.assertEqual(rc, 0)
        self.assertTrue(last_dedicated_tag(self.repo))

    def test_raw_reviewer_result_refuses_stamp(self):
        hostile = HostileRuntime(
            [emit({"_raw": "usage: claude"})],
            phases=("reviewer-fake",),
        )
        before = {row["tag"] for row in list_reviewed_tags(self.repo)}
        with register_runtime("fake", hostile):
            main(["--repo", str(self.repo), "--fake", "review", "--stamp", "--force"])
        after = {row["tag"] for row in list_reviewed_tags(self.repo)}
        self.assertEqual(after - before, set())
        self.assertEqual(last_dedicated_tag(self.repo), "")

    def test_pr_stamp_refused_when_gh_missing(self):
        env = os.environ.copy()
        env["PATH"] = self._path_without_gh()
        env["TEAM_HOME"] = str(ROOT)
        with mock.patch.dict(os.environ, env, clear=True):
            rc = main(
                ["--repo", str(self.repo), "--fake", "review", "--pr", "12", "--force"]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(last_dedicated_tag(self.repo), "")
        range_md = (
            self.repo / ".team" / "work" / "review-pr-12" / "range.md"
        ).read_text(encoding="utf-8")
        self.assertTrue(
            "merge-base" in range_md or "branch-fallback" in range_md or "fallback" in range_md,
            range_md,
        )

    def test_pr_stamp_refused_when_gh_exits_nonzero(self):
        bindir = self._install_gh("exit 1")
        env = os.environ.copy()
        env["PATH"] = self._path_with(bindir)
        env["TEAM_HOME"] = str(ROOT)
        with mock.patch.dict(os.environ, env, clear=True):
            rc = main(
                ["--repo", str(self.repo), "--fake", "review", "--pr", "4", "--force"]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(last_dedicated_tag(self.repo), "")

    def test_pr_stamp_refused_when_head_is_not_pr_head(self):
        bindir = self._install_gh(
            'echo "$*" | grep -q headRefOid && echo \'{"headRefOid":"0000000000000000000000000000000000000000"}\' && exit 0\n'
            'echo "$*" | grep -q "pr diff" && echo "diff --git a/README b/README" && exit 0\n'
            'echo "$*" | grep -q "pr view" && echo \'{"title":"x","commits":[]}\' && exit 0\n'
            "exit 1"
        )
        env = os.environ.copy()
        env["PATH"] = self._path_with(bindir)
        env["TEAM_HOME"] = str(ROOT)
        with mock.patch.dict(os.environ, env, clear=True):
            rc = main(
                ["--repo", str(self.repo), "--fake", "review", "--pr", "9", "--force"]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(last_dedicated_tag(self.repo), "")

    def test_pr_stamp_matching_head_creates_annotated_tag(self):
        head = head_sha(self.repo)
        bindir = self._install_gh(
            'echo "$*" | grep -q headRefOid && echo \'{"headRefOid":"%s"}\' && exit 0\n'
            'echo "$*" | grep -q "pr diff" && echo "diff --git a/README b/README\\n+++ b/README\\n+one" && exit 0\n'
            'echo "$*" | grep -q "pr view" && echo \'{"title":"x","commits":[]}\' && exit 0\n'
            "exit 1" % head
        )
        env = os.environ.copy()
        env["PATH"] = self._path_with(bindir)
        env["TEAM_HOME"] = str(ROOT)
        with mock.patch.dict(os.environ, env, clear=True):
            rc = main(
                ["--repo", str(self.repo), "--fake", "review", "--pr", "7", "--force"]
            )
        self.assertEqual(rc, 0)
        tag = last_dedicated_tag(self.repo)
        self.assertTrue(tag.startswith("reviewed-"), tag)
        self.assertEqual(_tag_type(self.repo, tag), "tag")
        self.assertEqual(git(self.repo, "rev-list", "-n", "1", tag).strip(), head)


if __name__ == "__main__":
    unittest.main()
