"""Range artifacts: one expression, three outputs; names derived from the patch.

Covers guardian findings on root-commit omission and names.txt vs working tree.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.config import load_config
from team.gitutil import commit_count, range_diff, range_log, range_name_only
from team.pipeline import start_range_review
from tests.support.repo import commit_file, git, init_repo

_DIFF_GIT = re.compile(r"^diff --git a/(.+) b/(.+)$", re.M)


def paths_from_patch(patch: str) -> list:
    found = set()
    for a, b in _DIFF_GIT.findall(patch):
        found.add(a)
        found.add(b)
    return sorted(found)


def log_shas(text: str) -> list:
    shas = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("("):
            continue
        shas.append(line.split()[0])
    return shas


def is_empty_log(text: str) -> bool:
    t = text.strip()
    return (not t) or t == "(empty range)"


def is_empty_diff(text: str) -> bool:
    t = text.strip()
    return (not t) or t == "(empty diff)"


def range_md_commits(text: str) -> int:
    for line in text.splitlines():
        stripped = line.strip().lstrip("- ").strip()
        if stripped.startswith("commits:"):
            return int(stripped.split(":", 1)[1].strip())
    raise AssertionError("range.md has no commits: line\n%s" % text)


def commit_paths(repo: Path, sha: str) -> set:
    out = git(repo, "diff-tree", "-r", "-m", "--name-only", "--no-commit-id", sha, check=False)
    return {p.strip() for p in out.splitlines() if p.strip()}


class RangeArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo, "one\n")
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self):
        return load_config(self.repo, fake=True, force=True)

    def _collect(self, slug="review-since-tag", **kwargs):
        return start_range_review(self._cfg(), slug=slug, **kwargs)

    def _artifacts(self, slug="review-since-tag"):
        work = self.repo / ".team" / "work" / slug
        return (
            work,
            (work / "git" / "log.txt").read_text(encoding="utf-8"),
            (work / "git" / "diff.patch").read_text(encoding="utf-8"),
            [
                p
                for p in (work / "git" / "names.txt").read_text(encoding="utf-8").splitlines()
                if p.strip()
            ],
            (work / "range.md").read_text(encoding="utf-8"),
        )

    def test_root_commit_only_repo_diff_contains_readme(self):
        """No-tag branch: empty-tree base. One commit adding README is in the diff."""
        pipe = self._collect()
        work, log, diff, names, range_md = self._artifacts()
        first = git(self.repo, "rev-list", "--max-parents=0", "HEAD").strip().splitlines()[0]
        self.assertEqual(len(log_shas(log)), 1)
        self.assertIn(first[:7], log)
        self.assertEqual(range_md_commits(range_md), 1)
        self.assertFalse(is_empty_diff(diff), "root commit must appear in diff.patch")
        self.assertIn("+++ b/README", diff)
        self.assertIn("+one", diff)
        self.assertEqual(names, ["README"])

    def test_log_nonempty_and_diff_empty_is_never_valid_branch(self):
        self._collect()
        _work, log, diff, _names, _range_md = self._artifacts()
        if not is_empty_log(log) and is_empty_diff(diff):
            self.fail("log.txt is non-empty but diff.patch is empty (branch)")

    def test_log_nonempty_and_diff_empty_is_never_valid_dedicated(self):
        git(self.repo, "tag", "reviewed-20200101-0000")
        commit_file(self.repo, "lib.py", "x\n", "second")
        self._collect()
        _work, log, diff, _names, _range_md = self._artifacts()
        if not is_empty_log(log) and is_empty_diff(diff):
            self.fail("log.txt is non-empty but diff.patch is empty (dedicated)")

    def test_log_nonempty_and_diff_empty_is_never_valid_since(self):
        commit_file(self.repo, "lib.py", "x\n", "second")
        since = git(self.repo, "rev-parse", "HEAD~1").strip()
        self._collect(since=since)
        _work, log, diff, _names, _range_md = self._artifacts()
        if not is_empty_log(log) and is_empty_diff(diff):
            self.fail("log.txt is non-empty but diff.patch is empty (since)")

    def test_log_nonempty_and_diff_empty_is_never_valid_pr_fallback(self):
        self._collect(slug="review-pr-1", pr="1")
        _work, log, diff, _names, range_md = self._artifacts("review-pr-1")
        if not is_empty_log(log) and is_empty_diff(diff):
            self.fail("log.txt is non-empty but diff.patch is empty (pr fallback)")

    def test_names_txt_derived_from_diff_patch_headers(self):
        commit_file(self.repo, "lib.py", "x\n", "second")
        self._collect()
        _work, _log, diff, names, _range_md = self._artifacts()
        self.assertEqual(names, paths_from_patch(diff))

    def test_names_txt_is_not_the_working_tree(self):
        commit_file(self.repo, "lib.py", "x\n", "second")
        (self.repo / "README").write_text("dirty working tree\n", encoding="utf-8")
        (self.repo / "staged-only.txt").write_text("not in range\n", encoding="utf-8")
        git(self.repo, "add", "--", "staged-only.txt")
        self._collect()
        _work, _log, diff, names, _range_md = self._artifacts()
        self.assertEqual(names, paths_from_patch(diff))
        self.assertNotIn("staged-only.txt", names)
        self.assertIn("README", names)
        self.assertIn("lib.py", names)

    def test_every_logged_commit_is_represented_in_names(self):
        commit_file(self.repo, "lib.py", "x\n", "second")
        self._collect()
        _work, log, _diff, names, _range_md = self._artifacts()
        name_set = set(names)
        for sha in log_shas(log):
            touched = commit_paths(self.repo, sha)
            missing = touched - name_set
            self.assertFalse(
                missing,
                "commit %s touches %s absent from names.txt" % (sha, sorted(missing)),
            )

    def test_range_md_commit_count_matches_log_and_rev_list(self):
        commit_file(self.repo, "lib.py", "x\n", "second")
        self._collect()
        _work, log, _diff, _names, range_md = self._artifacts()
        n_log = len(log_shas(log))
        self.assertEqual(range_md_commits(range_md), n_log)
        self.assertEqual(commit_count(self.repo, ""), n_log)

    def test_rename_includes_both_old_and_new_path(self):
        commit_file(self.repo, "old.txt", "same\n", "add old")
        git(self.repo, "mv", "old.txt", "new.txt")
        git(self.repo, "commit", "-m", "rename")
        self._collect()
        _work, _log, diff, names, _range_md = self._artifacts()
        self.assertEqual(names, paths_from_patch(diff))
        self.assertIn("old.txt", names)
        self.assertIn("new.txt", names)

    def test_mode_only_change_still_lists_the_path(self):
        commit_file(self.repo, "script.sh", "#!/bin/sh\n", "add script")
        git(self.repo, "update-index", "--chmod=+x", "script.sh")
        git(self.repo, "commit", "-m", "mode")
        git(self.repo, "checkout", "--", ".")
        self._collect()
        _work, _log, diff, names, _range_md = self._artifacts()
        self.assertEqual(names, paths_from_patch(diff))
        self.assertIn("script.sh", names)

    def test_merge_commit_files_are_in_names(self):
        commit_file(self.repo, "a.txt", "a\n", "on trunk")
        git(self.repo, "checkout", "-b", "other")
        commit_file(self.repo, "b.txt", "b\n", "on other")
        git(self.repo, "checkout", "-")
        git(self.repo, "merge", "--no-ff", "other", "-m", "merge other")
        self._collect()
        _work, log, _diff, names, _range_md = self._artifacts()
        name_set = set(names)
        for sha in log_shas(log):
            missing = commit_paths(self.repo, sha) - name_set
            self.assertFalse(missing, "merge-range commit %s missing %s" % (sha, sorted(missing)))
        self.assertIn("a.txt", names)
        self.assertIn("b.txt", names)

    def test_empty_range_is_valid_both_artifacts_empty(self):
        git(self.repo, "tag", "reviewed-20200101-0000")
        self._collect()
        _work, log, diff, names, range_md = self._artifacts()
        self.assertTrue(is_empty_log(log))
        self.assertTrue(is_empty_diff(diff))
        self.assertEqual(names, [])
        self.assertEqual(range_md_commits(range_md), 0)
        pipe = start_range_review(self._cfg(), slug="review-since-tag-2")
        pipe.run()
        self.assertEqual(pipe.state.stop_reason, "complete")
        self.assertTrue((pipe.work / "review.md").is_file())

    def test_range_diff_helper_includes_root_commit(self):
        diff = range_diff(self.repo, "")
        self.assertTrue(diff.strip(), "range_diff('', repo) must include the root commit")
        self.assertIn("README", diff)

    def test_range_name_only_helper_is_not_worktree_when_base_empty(self):
        (self.repo / "README").write_text("dirty\n", encoding="utf-8")
        names = range_name_only(self.repo, "")
        log = range_log(self.repo, "")
        self.assertTrue(log_shas(log))
        self.assertIn("README", names)
        self.assertEqual(names, paths_from_patch(range_diff(self.repo, "")))


if __name__ == "__main__":
    unittest.main()
