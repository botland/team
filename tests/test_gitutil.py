import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.gitutil import (
    GitError,
    already_dirty_mutations,
    changed_paths,
    delta_paths,
    name_status_paths,
    porcelain_paths,
    pr_bundle,
    product_paths,
    revert_product,
    snapshot,
    status_record_paths,
    submodule_paths,
    verify_delta,
)
from tests.support.repo import git, init_repo


class DeltaTests(unittest.TestCase):
    def test_delta(self):
        self.assertEqual(delta_paths(["a"], ["a", "tests/x.py"]), ["tests/x.py"])

    def test_verify_ok(self):
        ok, bad = verify_delta(
            ["tests/a.py", ".team/work/foo/design.md"],
            ["tests"],
            always_allowed=[".team/work"],
        )
        self.assertEqual(bad, [])
        self.assertEqual(len(ok), 2)

    def test_verify_violation(self):
        ok, bad = verify_delta(["src/a.py", "tests/a.py"], ["tests"])
        self.assertEqual(bad, ["src/a.py"])
        self.assertEqual(ok, ["tests/a.py"])

    def test_empty_root_advisory(self):
        ok, bad = verify_delta(["src/a.py"], [""])
        self.assertEqual(bad, [])
        self.assertEqual(ok, ["src/a.py"])

    def test_denied_roots_win_over_dot_allowed(self):
        ok, bad = verify_delta(
            [
                "ARCHITECTURE.md",
                "inferedge-phase1/.env.example",
                "tests/test_a.py",
                "appliance-console/page.tsx",
                ".team/work/s/x.md",
            ],
            ["."],
            always_allowed=[".team/work"],
            denied_roots=["tests", "appliance-console"],
        )
        self.assertEqual(
            bad, ["tests/test_a.py", "appliance-console/page.tsx"]
        )
        self.assertEqual(
            ok,
            [
                "ARCHITECTURE.md",
                "inferedge-phase1/.env.example",
                ".team/work/s/x.md",
            ],
        )

    def test_product_paths_drop_team_work(self):
        self.assertEqual(
            product_paths(["src/a.py", ".team/work/s/review.md", "tests/t.py"]),
            ["src/a.py", "tests/t.py"],
        )


class IgnoredMembershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        (self.repo / ".gitignore").write_text("secret.env\n*.ignored\n", encoding="utf-8")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-m", "ignore rule")

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_and_changed_paths_include_gitignored_create(self):
        before = snapshot(self.repo)
        (self.repo / "secret.env").write_text("ignored-bytes\n", encoding="utf-8")
        after = snapshot(self.repo)
        self.assertIn("secret.env", changed_paths(self.repo, before, after))

    def test_membership_functions_agree_on_ignored_path(self):
        before = snapshot(self.repo)
        (self.repo / "secret.env").write_text("ignored-bytes\n", encoding="utf-8")
        after = snapshot(self.repo)
        delta = changed_paths(self.repo, before, after)
        self.assertIn("secret.env", delta)
        revert_product(self.repo, {**before, "blobs": {}})
        after_restore = snapshot(self.repo)
        self.assertNotIn(
            "secret.env",
            changed_paths(self.repo, before, after_restore),
        )
        self.assertFalse((self.repo / "secret.env").exists())

    def test_already_dirty_is_run_start_not_hop_start(self):
        origin = {"NOTES": "h0"}
        before = {"NOTES": "h0", "src/a.py": "h1"}
        after = {"NOTES": "h0", "src/a.py": "h2"}
        self.assertEqual(
            already_dirty_mutations(
                ["src/a.py"], origin, before, after
            ),
            [],
        )
        self.assertEqual(
            already_dirty_mutations(
                ["NOTES"],
                origin,
                {"NOTES": "h0"},
                {"NOTES": "h1"},
            ),
            ["NOTES"],
        )

    def test_already_dirty_skips_work_root_and_cleaned_paths(self):
        origin = {"src/a.py": "h0", ".team/work/s/x": "w0"}
        self.assertEqual(
            already_dirty_mutations(
                [".team/work/s/x"],
                origin,
                {".team/work/s/x": "w0"},
                {".team/work/s/x": "w1"},
                exempt_roots=(".team/work",),
            ),
            [],
        )
        self.assertEqual(
            already_dirty_mutations(
                ["src/a.py"],
                origin,
                {},
                {"src/a.py": "h2"},
            ),
            [],
        )


class RenameTotalityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_a.py").write_text("ok\n", encoding="utf-8")
        git(self.repo, "add", "--", "tests/test_a.py")
        git(self.repo, "commit", "-m", "seed test")

    def tearDown(self):
        self.tmp.cleanup()

    def _assert_rename_porcelain(self, src: str, dest: str) -> str:
        status = git(self.repo, "status", "--porcelain", "-uall", check=False)
        self.assertTrue(
            any(" -> " in line for line in status.splitlines()),
            "setup must produce porcelain rename/copy, got:\n%s" % status,
        )
        return status

    def test_changed_paths_rename_without_head_move_includes_source_and_dest(self):
        before = snapshot(self.repo)
        git(self.repo, "mv", "--", "tests/test_a.py", "src/stolen.py")
        self._assert_rename_porcelain("tests/test_a.py", "src/stolen.py")
        after = snapshot(self.repo)
        self.assertFalse((self.repo / "tests" / "test_a.py").exists())
        self.assertTrue((self.repo / "src" / "stolen.py").is_file())
        name_status = git(
            self.repo, "diff", "--name-status", "--no-renames", "HEAD", check=False
        )
        self.assertIn("tests/test_a.py", name_status)
        self.assertIn("src/stolen.py", name_status)
        delta = changed_paths(self.repo, before, after)
        self.assertIn("tests/test_a.py", delta)
        self.assertIn("src/stolen.py", delta)
        keys = set(porcelain_paths(self.repo)) | set((after.get("entries") or {}))
        self.assertIn("tests/test_a.py", set(delta) | keys)
        self.assertIn("src/stolen.py", set(delta) | keys)

    def test_changed_paths_rename_after_commit_still_includes_source_and_dest(self):
        before = snapshot(self.repo)
        git(self.repo, "mv", "--", "tests/test_a.py", "src/stolen.py")
        git(self.repo, "commit", "-m", "rename")
        after = snapshot(self.repo)
        delta = changed_paths(self.repo, before, after)
        self.assertIn("tests/test_a.py", delta)
        self.assertIn("src/stolen.py", delta)

    def test_status_records_rename_includes_source_and_dest(self):
        # -z: "XY dest" then a bare record with the source. No quoting.
        got = status_record_paths(["R  new name.py", "old name.py", " M plain.py"])
        self.assertEqual(got, ["new name.py", "old name.py", "plain.py"])

    def test_status_records_copy_is_dest_only(self):
        got = status_record_paths(["C  copy dest.py", "copy src.py"])
        self.assertEqual(got, ["copy dest.py"])

    def test_name_status_records_keep_rename_source_and_copy_dest(self):
        got = name_status_paths(
            ["M", "a -> b", "R100", "old.py", "new.py", "C075", "src.py", "dst.py"]
        )
        self.assertEqual(got, ["a -> b", "new.py", "old.py", "dst.py"])

    def test_hostile_names_survive_the_fence_reader(self):
        """The fence decides root membership from these strings.

        A name with non-ASCII bytes, an embedded " -> ", or a quote used to
        arrive C-quoted or truncated, so _content_id looked at a path that
        does not exist and the before/after comparison for it was vacuous.
        """
        hostile = ["café.py", "a -> b", 'q"uote.py', "sp ace.py"]
        for name in hostile:
            (self.repo / name).write_text("x\n", encoding="utf-8")
        before = snapshot(self.repo)
        paths = porcelain_paths(self.repo)
        for name in hostile:
            self.assertIn(name, paths, paths)
            self.assertNotEqual(
                (before.get("entries") or {}).get(name),
                "missing",
                "%s exists on disk but the snapshot could not find it" % name,
            )
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "hostile")
        git(self.repo, "mv", "--", "café.py", "café renamed.py")
        git(self.repo, "commit", "-m", "rename")
        after = snapshot(self.repo)
        delta = changed_paths(self.repo, before, after)
        self.assertIn("café.py", delta)
        self.assertIn("café renamed.py", delta)

    def _porcelain_with_status(self, records):
        """porcelain_paths over a canned -z status blob; other git calls are real."""
        import team.gitutil as gu

        orig = gu.git

        def fake_git(repo, *args, check=True):
            if args[:2] == ("status", "--porcelain"):
                self.assertIn("-z", args, "porcelain must be read as -z records")
                return "\0".join(records) + "\0"
            return orig(repo, *args, check=check)

        gu.git = fake_git
        try:
            return porcelain_paths(self.repo)
        finally:
            gu.git = orig

    def test_porcelain_rename_blob_names_both_sides(self):
        paths = self._porcelain_with_status(
            ["R  new", "old", "RM new name.py", "old name.py"]
        )
        for rel in ("old", "new", "old name.py", "new name.py"):
            self.assertIn(rel, paths, "porcelain rename blob missed %r in %s" % (rel, paths))

    def test_porcelain_copy_blob_is_dest_only(self):
        paths = self._porcelain_with_status(
            ["C  copy_dest", "copy_src", "CM copy dest.py", "copy src.py"]
        )
        self.assertIn("copy_dest", paths, paths)
        self.assertIn("copy dest.py", paths, paths)
        self.assertNotIn("copy_src", paths, paths)

    def test_porcelain_rename_space_and_score_and_copy(self):
        # Real git mv is rename (R), never isolated copy. Copy dest-in-delta
        # lives in the C-only tests above; this case must not mix R+C lines.
        (self.repo / "old name.py").write_text("spaced\n", encoding="utf-8")
        git(self.repo, "add", "--", "old name.py")
        git(self.repo, "commit", "-m", "spaced")
        before = snapshot(self.repo)
        git(self.repo, "mv", "--", "old name.py", "new name.py")
        self._assert_rename_porcelain("old name.py", "new name.py")
        after = snapshot(self.repo)
        delta = changed_paths(self.repo, before, after)
        self.assertIn("old name.py", delta)
        self.assertIn("new name.py", delta)

    def test_changed_paths_plain_delete_still_visible(self):
        before = snapshot(self.repo)
        (self.repo / "tests" / "test_a.py").unlink()
        after = snapshot(self.repo)
        self.assertIn("tests/test_a.py", changed_paths(self.repo, before, after))


class SubmodulePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_gitmodules_and_gitlinks(self):
        (self.repo / ".gitmodules").write_text(
            '[submodule "appliance-console"]\n'
            "\tpath = appliance-console\n"
            "\turl = git@example.com:console.git\n"
            '[submodule "appliance-support"]\n'
            "\tpath = appliance-support\n"
            "\turl = git@example.com:support.git\n",
            encoding="utf-8",
        )
        git(self.repo, "add", ".gitmodules")
        git(self.repo, "commit", "-m", "gitmodules")
        sha = git(self.repo, "rev-parse", "HEAD").strip()
        git(
            self.repo,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000,%s,vendor-link" % sha,
        )
        self.assertEqual(
            submodule_paths(self.repo),
            ["appliance-console", "appliance-support", "vendor-link"],
        )


class RevertProductTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "keep.py").write_text("keep\n", encoding="utf-8")
        git(self.repo, "add", "src/keep.py")
        git(self.repo, "commit", "-m", "seed")

    def tearDown(self):
        self.tmp.cleanup()

    def test_revert_raises_when_reset_fails(self):
        (self.repo / "src" / "keep.py").write_text("pwned\n", encoding="utf-8")
        git(self.repo, "add", "src/keep.py")
        git(self.repo, "commit", "-m", "hostile")
        after = snapshot(self.repo)
        self.assertTrue(after.get("head"))
        before = {
            "head": "0" * 40,
            "paths": [],
            "entries": {},
            "blobs": {},
        }
        with self.assertRaises(GitError):
            revert_product(self.repo, before)
        self.assertEqual(
            (self.repo / "src" / "keep.py").read_text(encoding="utf-8"),
            "pwned\n",
        )
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), after["head"])

    def test_revert_restores_blobs_when_reset_fails_without_checkout(self):
        keep = self.repo / "src" / "keep.py"
        before_head = git(self.repo, "rev-parse", "HEAD").strip()
        before = {
            "head": before_head,
            "paths": ["src/keep.py"],
            "entries": {"src/keep.py": "old"},
            "blobs": {"src/keep.py": b"keep\n"},
        }
        keep.write_text("pwned\n", encoding="utf-8")
        git(self.repo, "add", "src/keep.py")
        git(self.repo, "commit", "-m", "hostile")
        hostile_head = git(self.repo, "rev-parse", "HEAD").strip()

        import team.gitutil as gu

        orig = gu.git

        def wrapped(repo, *args, check=True):
            if args[:2] == ("reset", "--mixed"):
                raise GitError("reset refused")
            return orig(repo, *args, check=check)

        gu.git = wrapped
        try:
            with self.assertRaises(GitError) as ctx:
                revert_product(self.repo, before)
            self.assertIn("reset refused", str(ctx.exception))
        finally:
            gu.git = orig

        self.assertEqual(keep.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").strip(), hostile_head)
        self.assertTrue(keep.is_file())


def _oneline_shas(log):
    out = []
    for line in (log or "").splitlines():
        text = line.strip()
        if not text or text.startswith("("):
            continue
        out.append(text.split()[0])
    return out


class PrBundleHowTests(unittest.TestCase):
    """Seam: gh JSON ↔ git oneline. how=gh requires a commit list."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def _bundle(self, view_code, view_stdout, diff="diff --git a/x b/x\n+++ b/x\n+x\n"):
        import subprocess
        from unittest import mock

        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            argv = list(cmd)
            if argv and argv[0] == "gh" and "diff" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=diff, stderr="")
            if argv and argv[0] == "gh" and "view" in argv:
                return subprocess.CompletedProcess(
                    argv, view_code, stdout=view_stdout, stderr="view failed"
                )
            return real_run(cmd, **kwargs)

        with mock.patch("subprocess.run", side_effect=fake_run):
            return pr_bundle(self.repo, "12")

    def _assert_how_gh_has_commit_list(self, log, diff, how):
        if how != "gh":
            self.assertTrue(
                how == "branch-fallback" or str(how).startswith("merge-base:"),
                how,
            )
            return
        stripped = (log or "").strip()
        self.assertFalse(stripped.startswith("{"), log)
        self.assertNotEqual(stripped, "", "how=gh must not publish an empty commit list")
        self.assertNotEqual(stripped, "(empty range)")
        self.assertTrue(_oneline_shas(log), log)
        self.assertTrue((diff or "").strip(), "how=gh range still carries the PR diff")

    def test_how_gh_view_failure_still_has_a_commit_list(self):
        log, diff, how = self._bundle(1, "")
        self._assert_how_gh_has_commit_list(log, diff, how)

    def test_how_gh_json_decode_error_still_has_a_commit_list(self):
        log, diff, how = self._bundle(0, "not-json{")
        self._assert_how_gh_has_commit_list(log, diff, how)

    def test_how_gh_missing_oid_still_has_a_commit_list(self):
        log, diff, how = self._bundle(
            0, '{"title":"PR","commits":[{"messageHeadline":"x"}]}'
        )
        self._assert_how_gh_has_commit_list(log, diff, how)

    def test_how_gh_unexpected_shape_still_has_a_commit_list(self):
        log, diff, how = self._bundle(0, '[{"oid":"abc"}]')
        self._assert_how_gh_has_commit_list(log, diff, how)

    def test_how_gh_with_commits_is_oneline_not_json(self):
        import json

        view = json.dumps(
            {
                "title": "PR",
                "commits": [
                    {"oid": "abc123def456aaa", "messageHeadline": "first"},
                    {"oid": "def456abc789bbb", "messageHeadline": "second"},
                ],
            },
            separators=(",", ":"),
        )
        self.assertNotIn("\n", view)
        log, diff, how = self._bundle(0, view)
        self.assertEqual(how, "gh")
        self.assertNotEqual(log.strip(), view)
        self.assertFalse(log.strip().startswith("{"), log)
        self.assertNotIn('"commits"', (log or "").splitlines()[0] if log.strip() else "")
        shas = _oneline_shas(log)
        self.assertEqual(len(shas), 2, log)
        self.assertTrue(shas[0].startswith("abc123def456") or "abc123def456" in log, log)
        self.assertIn("first", log)
        self.assertIn("second", log)
        self.assertTrue((diff or "").strip())


if __name__ == "__main__":
    unittest.main()
