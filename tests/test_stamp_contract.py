"""A reviewed-* tag is evidence of a completed review, not a bare watermark."""

from __future__ import annotations

import json
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
from team.gitutil import last_dedicated_tag, list_reviewed_tags, paths_from_diff
from tests.support.hostile import HostileRuntime, emit, register_runtime
from tests.support.repo import git, head_sha, init_repo


def _log_shas(text: str) -> list:
    shas = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("("):
            continue
        shas.append(line.split()[0])
    return shas


def _range_md_commits(text: str) -> int:
    for line in text.splitlines():
        stripped = line.strip().lstrip("- ").strip()
        if stripped.startswith("commits:"):
            return int(stripped.split(":", 1)[1].strip())
    raise AssertionError("range.md has no commits: line\n%s" % text)


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

    def _pr_work(self, pr: str) -> Path:
        return self.repo / ".team" / "work" / ("review-pr-%s" % pr)

    def _repo_commits(self) -> list:
        return git(self.repo, "rev-list", "--reverse", "HEAD").strip().splitlines()

    def _view_json(self, oids=None, headlines=None) -> str:
        commits = []
        for i, oid in enumerate(oids or self._repo_commits()):
            msg = "c%d" % i
            if headlines and i < len(headlines):
                msg = headlines[i]
            commits.append({"oid": oid, "messageHeadline": msg})
        return json.dumps({"title": "x", "commits": commits}, separators=(",", ":"))

    def _install_gh_pr(self, *, head_oid: str, diff: str, view_json: str) -> Path:
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir(exist_ok=True)
        (bindir / "head.json").write_text(
            json.dumps({"headRefOid": head_oid}), encoding="utf-8"
        )
        (bindir / "pr.diff").write_text(diff, encoding="utf-8")
        (bindir / "view.json").write_text(view_json, encoding="utf-8")
        gh = bindir / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            'echo "$*" | grep -q headRefOid && cat "%s" && exit 0\n'
            'echo "$*" | grep -q "pr diff" && cat "%s" && exit 0\n'
            'echo "$*" | grep -q "pr view" && cat "%s" && exit 0\n'
            "exit 1\n"
            % (bindir / "head.json", bindir / "pr.diff", bindir / "view.json"),
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        return bindir

    def _assert_log_is_oneline_commits(self, log: str, oids=None) -> list:
        stripped = (log or "").strip()
        self.assertTrue(stripped, "git/log.txt must be a commit list")
        self.assertFalse(stripped.startswith("{"), log)
        self.assertFalse(stripped.startswith("["), log)
        first = stripped.splitlines()[0]
        self.assertNotIn('"commits"', first, log)
        listed = _log_shas(log)
        self.assertTrue(listed, log)
        if oids is not None:
            self.assertEqual(len(listed), len(oids), log)
            for sha, oid in zip(listed, oids):
                self.assertTrue(
                    oid.startswith(sha) or sha.startswith(oid[:7]),
                    "log sha %s is not PR commit %s" % (sha, oid),
                )
        return listed

    def _assert_pr_collect_is_commit_set(self, pr: str, *, oids, diff: str) -> None:
        work = self._pr_work(pr)
        log = (work / "git" / "log.txt").read_text(encoding="utf-8")
        patch = (work / "git" / "diff.patch").read_text(encoding="utf-8")
        names = [
            p
            for p in (work / "git" / "names.txt").read_text(encoding="utf-8").splitlines()
            if p.strip()
        ]
        range_md = (work / "range.md").read_text(encoding="utf-8")
        listed = self._assert_log_is_oneline_commits(log, oids)
        self.assertEqual(names, paths_from_diff(diff))
        self.assertEqual(names, paths_from_diff(patch))
        self.assertEqual(_range_md_commits(range_md), len(listed))

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
        log = (self._pr_work("12") / "git" / "log.txt").read_text(encoding="utf-8")
        self._assert_log_is_oneline_commits(log)

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
        log = (self._pr_work("4") / "git" / "log.txt").read_text(encoding="utf-8")
        self._assert_log_is_oneline_commits(log)

    def test_pr_stamp_refused_when_head_is_not_pr_head(self):
        oids = self._repo_commits()
        diff = "diff --git a/README b/README\n+++ b/README\n+one\n"
        bindir = self._install_gh_pr(
            head_oid="0000000000000000000000000000000000000000",
            diff=diff,
            view_json=self._view_json(oids, headlines=["first"]),
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
        self._assert_pr_collect_is_commit_set("9", oids=oids, diff=diff)

    def test_pr_stamp_matching_head_creates_annotated_tag(self):
        head = head_sha(self.repo)
        oids = self._repo_commits()
        diff = "diff --git a/README b/README\n+++ b/README\n+one\n"
        bindir = self._install_gh_pr(
            head_oid=head,
            diff=diff,
            view_json=self._view_json(oids, headlines=["first"]),
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
        self._assert_pr_collect_is_commit_set("7", oids=oids, diff=diff)

    def test_pr_collect_log_is_oneline_not_gh_json(self):
        """Stamp/tag success is not PR membership. log.txt must be the commit set."""
        head = head_sha(self.repo)
        oids = self._repo_commits()
        self.assertTrue(oids)
        view = self._view_json(oids, headlines=["first"])
        self.assertNotIn("\n", view)
        self.assertTrue(view.startswith("{"))
        diff = "diff --git a/README b/README\n+++ b/README\n+one\n"
        bindir = self._install_gh_pr(head_oid=head, diff=diff, view_json=view)
        env = os.environ.copy()
        env["PATH"] = self._path_with(bindir)
        env["TEAM_HOME"] = str(ROOT)
        with mock.patch.dict(os.environ, env, clear=True):
            rc = main(
                ["--repo", str(self.repo), "--fake", "review", "--pr", "3", "--force"]
            )
        self.assertEqual(rc, 0)
        work = self._pr_work("3")
        log = (work / "git" / "log.txt").read_text(encoding="utf-8")
        self.assertNotEqual(log.strip(), view)
        self._assert_pr_collect_is_commit_set("3", oids=oids, diff=diff)

    def test_pr_empty_commits_array_is_not_how_gh_json_log(self):
        """commits:[] + a real gh diff must not publish JSON (or empty) as log."""
        head = head_sha(self.repo)
        empty = json.dumps({"title": "x", "commits": []}, separators=(",", ":"))
        diff = "diff --git a/README b/README\n+++ b/README\n+one\n"
        bindir = self._install_gh_pr(head_oid=head, diff=diff, view_json=empty)
        env = os.environ.copy()
        env["PATH"] = self._path_with(bindir)
        env["TEAM_HOME"] = str(ROOT)
        with mock.patch.dict(os.environ, env, clear=True):
            rc = main(
                ["--repo", str(self.repo), "--fake", "review", "--pr", "8", "--force"]
            )
        self.assertEqual(rc, 0)
        work = self._pr_work("8")
        log = (work / "git" / "log.txt").read_text(encoding="utf-8")
        self._assert_log_is_oneline_commits(log)
        names = [
            p
            for p in (work / "git" / "names.txt").read_text(encoding="utf-8").splitlines()
            if p.strip()
        ]
        patch = (work / "git" / "diff.patch").read_text(encoding="utf-8")
        self.assertEqual(names, paths_from_diff(patch))
        self.assertTrue(names, "PR names.txt must come from the collected patch")


if __name__ == "__main__":
    unittest.main()
