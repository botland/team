import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.testhost import (
    collection_failed,
    compare,
    discover_test_command,
    is_product_fail,
    needs_repair,
    parse_failing_names,
    run_suite,
    suite_completed,
)


class CompareTests(unittest.TestCase):
    def test_regression(self):
        base = {"status": "PASS", "failing": []}
        final = {"status": "FAIL", "failing": ["tests/a.py"]}
        self.assertEqual(compare(base, final)["verdict"], "REGRESSION")

    def test_broken_baseline(self):
        base = {"status": "FAIL", "failing": ["old"]}
        final = {"status": "FAIL", "failing": ["old"]}
        self.assertEqual(compare(base, final)["verdict"], "BROKEN_BASELINE")

    def test_unverified(self):
        base = {"status": "UNVERIFIED", "failing": []}
        final = {"status": "UNVERIFIED", "failing": []}
        self.assertEqual(compare(base, final)["verdict"], "UNVERIFIED")

    def test_compare_final_fail_is_not_unverified_when_baseline_never_ran(self):
        named_fail = {"status": "FAIL", "failing": ["tests/a.py"]}
        unparsed_fail = {"status": "FAIL", "failing": [], "exit": 1}
        matrix = (
            ({"status": "UNVERIFIED", "failing": []}, named_fail, {"UNVERIFIED"}, {"FAIL", "REGRESSION"}),
            ({"status": "UNVERIFIED", "failing": []}, unparsed_fail, {"UNVERIFIED"}, {"FAIL"}),
            (
                {"status": "UNVERIFIED", "failing": []},
                {"status": "PASS", "failing": []},
                set(),
                {"PASS"},
            ),
            (
                {"status": "UNVERIFIED", "failing": []},
                {"status": "UNVERIFIED", "failing": []},
                set(),
                {"UNVERIFIED"},
            ),
            (
                {"status": "PASS", "failing": []},
                {
                    "status": "UNVERIFIED",
                    "failing": [],
                    "collection_failed": True,
                },
                set(),
                {"UNVERIFIED"},
            ),
            (
                {"status": "PASS", "failing": []},
                named_fail,
                set(),
                {"REGRESSION"},
            ),
        )
        for base, final, forbidden, allowed in matrix:
            with self.subTest(base=base["status"], final=final["status"], names=final.get("failing")):
                cmp = compare(base, final)
                self.assertEqual(cmp["baseline_status"], base["status"])
                self.assertEqual(cmp["final_status"], final["status"])
                self.assertNotIn(cmp["verdict"], forbidden)
                self.assertIn(cmp["verdict"], allowed)
                if final["status"] == "FAIL":
                    self.assertNotEqual(cmp["verdict"], "UNVERIFIED")
                    self.assertFalse(needs_repair({"status": "UNVERIFIED"}))
                    self.assertTrue(needs_repair(final))

    def test_parse_pytest(self):
        log = "FAILED tests/test_a.py::test_x\nFAILED tests/test_b.py::test_y\n"
        self.assertEqual(
            parse_failing_names(log),
            ["tests/test_a.py::test_x", "tests/test_b.py::test_y"],
        )

    def test_compare_unparsed_fail_is_not_regression(self):
        base = {"status": "PASS", "failing": []}
        final = {"status": "FAIL", "exit": 1, "failing": []}
        cmp = compare(base, final)
        self.assertNotEqual(cmp["verdict"], "REGRESSION")
        self.assertEqual(cmp["verdict"], "FAIL")
        self.assertTrue(
            _unparsed_signal(cmp),
            "FAIL with no parsed names must carry an unparsed/unknown-names signal, got %r"
            % cmp,
        )

    def test_collection_death_is_unverified_not_fail(self):
        log = (
            "ERROR collecting tests/unit/test_artifacts.py\n"
            "E   ModuleNotFoundError: No module named 'huggingface_hub'\n"
            "!!!!!!!!!!!!!!!!!!! Interrupted: 1 errors during collection !!!!!!!!!!!!!!!!!!!\n"
        )
        self.assertTrue(collection_failed(log))
        self.assertFalse(needs_repair({"status": "UNVERIFIED", "collection_failed": True}))
        self.assertTrue(needs_repair({"status": "FAIL", "failing": ["tests/a.py"]}))
        self.assertFalse(needs_repair({"status": "PASS"}))
        self.assertFalse(needs_repair({"status": "UNVERIFIED"}))
        cmp = compare(
            {"status": "PASS", "failing": []},
            {
                "status": "UNVERIFIED",
                "exit": 2,
                "failing": [],
                "collection_failed": True,
                "output": log,
            },
        )
        self.assertEqual(cmp["verdict"], "UNVERIFIED")
        self.assertNotEqual(cmp["verdict"], "REGRESSION")

    def test_conftest_import_error_is_collection_death(self):
        log = (
            "ImportError while loading conftest "
            "'/home/trader/ownedge/inferedge-phase1/tests/integration/conftest.py'.\n"
            "tests/integration/conftest.py:11: in <module>\n"
            "    from tests.helpers.orchestration_helpers import install_reconcile_patches\n"
            "E   ModuleNotFoundError: No module named 'huggingface_hub'\n"
        )
        self.assertTrue(collection_failed(log))
        self.assertTrue(collection_failed(log, 4))
        self.assertTrue(collection_failed("pytest: error: unrecognized arguments", 4))
        self.assertFalse(collection_failed("FAILED tests/a.py::test_x\n", 1))
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        cmd = (
            "python3 -c \"print('ImportError while loading conftest "
            "\\'/tmp/conftest.py\\'.');\n"
            "print('E   ModuleNotFoundError: No module named \\'huggingface_hub\\''); "
            "raise SystemExit(4)\""
        )
        run = run_suite(repo, cmd, timeout=30)
        self.assertEqual(run.get("status"), "UNVERIFIED", run)
        self.assertTrue(run.get("collection_failed"), run)
        self.assertFalse(is_product_fail(run))
        self.assertFalse(is_product_fail(status=run.get("status")))
        self.assertFalse(needs_repair(run))

    def test_run_suite_collection_death_is_unverified(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        cmd = (
            "python3 -c \"print('ERROR collecting tests/unit/test_x.py\\n"
            "Interrupted: 1 errors during collection'); raise SystemExit(2)\""
        )
        run = run_suite(repo, cmd, timeout=30)
        self.assertEqual(run.get("status"), "UNVERIFIED", run)
        self.assertTrue(run.get("collection_failed"), run)
        self.assertFalse(is_product_fail(run))
        self.assertFalse(is_product_fail(status=run.get("status")))
        self.assertFalse(needs_repair(run))

    def test_run_suite_collection_death_that_also_printed_names_is_unverified(self):
        """Parsed failing names do not upgrade a collection death to FAIL.

        The property is "the runner never executed cases", and that is decided
        by the collection language, not by whether the log happened to contain
        something name-shaped. The fixture prints a real FAILED line *and* dies
        in collection so the two signals genuinely disagree -- without the
        assertion below the test could pass on an empty name list and prove
        nothing.
        """
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        log_body = (
            "ERROR collecting tests/conftest.py\\n"
            "FAILED tests/test_x.py::test_y\\n"
            "!!!!!!!!!!!!!!!!!!! Interrupted: 1 errors during collection !!!!!!!!!!!!!!!!!!!"
        )
        cmd = "python3 -c \"print('%s'); raise SystemExit(2)\"" % log_body
        run = run_suite(repo, cmd, timeout=30)
        self.assertTrue(
            parse_failing_names(run.get("output") or ""),
            "fixture must parse at least one name, or this asserts nothing",
        )
        self.assertEqual(run.get("status"), "UNVERIFIED", run)
        self.assertTrue(run.get("collection_failed"), run)
        self.assertFalse(is_product_fail(run), run)
        self.assertFalse(is_product_fail(status=run.get("status")))
        self.assertFalse(needs_repair(run))

    def test_completed_suite_banner_is_not_collection_death(self):
        """Assertion dumps that reprint testhost source are not UNVERIFIED.

        A finished unittest/pytest banner wins over 'errors during collection'
        appearing inside the compared blob. This run's apply-test-report had
        Ran 461 tests / FAILED (failures=3) and was still marked collection
        death.
        """
        dump = (
            "AssertionError: 'def test_already_dirty_edit_is_a_violation' "
            "unexpectedly found in 'def collection_failed... errors during "
            "collection ... ERROR collecting tests/x.py'\n"
        )
        log = (
            dump
            + "\n----------------------------------------------------------------------\n"
            "Ran 461 tests in 60.158s\n\n"
            "FAILED (failures=3)\n"
        )
        self.assertTrue(suite_completed(log))
        self.assertFalse(
            collection_failed(log),
            "finished suite banner must beat collection phrases in the dump",
        )
        names = parse_failing_names(log)
        self.assertNotIn("(failures=3)", names)
        self.assertTrue(
            all(len(n) <= 201 for n in names),
            "failing names must not be the whole compared blob: %r" % names,
        )
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        cmd = (
            "python3 -c \"import sys; sys.stdout.write(%r); raise SystemExit(2)\""
            % log
        )
        run = run_suite(repo, cmd, timeout=30)
        self.assertFalse(run.get("collection_failed"), run)
        self.assertEqual(run.get("status"), "FAIL", run)
        self.assertTrue(is_product_fail(run), run)
        self.assertTrue(needs_repair(run))

    def test_parse_failing_names_skips_unittest_summary(self):
        log = "FAIL: test_x (mod.Cls)\nFAILED (failures=3)\n"
        names = parse_failing_names(log)
        self.assertIn("test_x", names)
        self.assertNotIn("(failures=3)", names)

    def test_run_suite_no_command_is_unverified(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        run = run_suite(repo, "", timeout=30)
        self.assertEqual(run.get("status"), "UNVERIFIED", run)
        self.assertFalse(needs_repair(run))
        self.assertFalse(is_product_fail(run))
        self.assertFalse(is_product_fail(status=run.get("status")))
        self.assertIn("no test command", (run.get("output") or "").lower())

    def test_run_suite_timeout_is_unverified(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        cmd = "python3 -c 'import time; time.sleep(30)'"
        try:
            run = run_suite(repo, cmd, timeout=1)
        except subprocess.TimeoutExpired as exc:
            self.fail("TimeoutExpired escaped run_suite: %s" % exc)
        self.assertIsInstance(run, dict)
        self.assertEqual(run.get("status"), "UNVERIFIED", run)
        self.assertFalse(is_product_fail(run), run)
        self.assertFalse(needs_repair(run), run)
        self.assertFalse(run.get("runner_missing"), run)

    def test_run_suite_timeout_zero_does_not_mean_immediate_unverified(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        for bound in (0, -1):
            with self.subTest(timeout=bound):
                run = run_suite(repo, "python3 -c 'pass'", timeout=bound)
                self.assertEqual(run.get("status"), "PASS", run)
                self.assertNotEqual(run.get("status"), "UNVERIFIED")

    def test_run_suite_product_fail_still_fail_when_under_timeout(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        run = run_suite(repo, "false", timeout=30)
        self.assertEqual(run.get("status"), "FAIL", run)
        self.assertTrue(is_product_fail(run), run)
        self.assertNotEqual(run.get("status"), "UNVERIFIED")

    def test_compare_timeout_unverified_is_unverified_not_regression(self):
        timeout_run = {
            "status": "UNVERIFIED",
            "failing": [],
            "runner_missing": False,
            "collection_failed": False,
        }
        cmp = compare({"status": "PASS", "failing": []}, timeout_run)
        self.assertEqual(cmp["verdict"], "UNVERIFIED")
        self.assertNotEqual(cmp["verdict"], "REGRESSION")

    def test_run_suite_product_fail_is_fail(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        run = run_suite(repo, "false", timeout=30)
        self.assertEqual(run.get("status"), "FAIL", run)
        self.assertFalse(run.get("collection_failed"), run)
        self.assertTrue(is_product_fail(run), run)
        self.assertTrue(is_product_fail(status=run.get("status")))
        self.assertTrue(needs_repair(run))

    def test_product_fail_is_the_only_repair_and_seq_failure(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        named = run_suite(
            repo,
            "python3 -c \"print('FAILED tests/a.py::test_x'); raise SystemExit(1)\"",
            timeout=30,
        )
        self.assertEqual(named.get("status"), "FAIL", named)
        self.assertTrue(needs_repair(named))
        self.assertTrue(is_product_fail(named))
        unparsed = run_suite(repo, "false", timeout=30)
        self.assertEqual(unparsed.get("status"), "FAIL", unparsed)
        self.assertTrue(needs_repair(unparsed))
        self.assertNotEqual(unparsed.get("status"), "UNVERIFIED")
        unverified = run_suite(repo, "", timeout=30)
        self.assertEqual(unverified.get("status"), "UNVERIFIED")
        self.assertFalse(needs_repair(unverified))
        self.assertFalse(is_product_fail(unverified))

    def test_collection_death_routes_as_unverified_not_repair(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        cmd = (
            "python3 -c \"print('ERROR collecting tests/unit/test_x.py\\n"
            "Interrupted: 1 errors during collection'); raise SystemExit(2)\""
        )
        run = run_suite(repo, cmd, timeout=30)
        self.assertEqual(run.get("status"), "UNVERIFIED", run)
        self.assertFalse(needs_repair(run))
        self.assertFalse(is_product_fail(run))

    def test_collection_failed_phrase_list_is_not_suite_never_ran(self):
        """Listed phrases are an approximation. Unlisted runner death stays open."""
        listed = (
            "ERROR collecting tests/unit/test_x.py\n"
            "Interrupted: 1 errors during collection\n"
        )
        self.assertTrue(collection_failed(listed))
        unlisted = "jest: the test suite failed to run\nmake: *** [test] Error 127\n"
        self.assertFalse(
            collection_failed(unlisted),
            "unlisted runner death must not be sold as collection_failed=True",
        )
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        # Ordinary failure exit: no collection language and no missing-runner
        # signal, so it stays a loud product FAIL rather than a silent skip.
        cmd = (
            "python3 -c \"print('jest: the test suite failed to run'); "
            "print('make: *** [test] Error 1'); raise SystemExit(1)\""
        )
        run = run_suite(repo, cmd, timeout=30)
        self.assertFalse(run.get("collection_failed"), run)
        self.assertFalse(run.get("runner_missing"), run)
        self.assertEqual(run.get("status"), "FAIL", run)
        self.assertTrue(needs_repair(run))

    def test_exit_127_is_a_missing_runner_not_a_product_fail(self):
        """The seam: collection_failed's phrase list and runner_unavailable's
        exit-code rule are two different routes to UNVERIFIED.

        127 is the POSIX command-not-found code, and make propagates a missing
        runner's 127 as its own. Calling that FAIL sends the debugger after a
        suite that never executed a case.
        """
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        cmd = (
            "python3 -c \"print('jest: the test suite failed to run'); "
            "print('make: *** [test] Error 127'); raise SystemExit(127)\""
        )
        run = run_suite(repo, cmd, timeout=30)
        self.assertFalse(run.get("collection_failed"), run)
        self.assertTrue(run.get("runner_missing"), run)
        self.assertEqual(run.get("status"), "UNVERIFIED", run)
        self.assertFalse(needs_repair(run))

    def test_exit_without_collection_language_is_product_fail(self):
        """Exit 4/5 with no collection text is a product FAIL, not UNVERIFIED.

        pytest uses 4/5 for usage / no-tests-collected. make, npm, and other
        wrappers may use those codes for product failure. collection_failed
        must not treat the exit code alone as collection death; a false
        UNVERIFIED is a silent --seq apply.
        """
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        log = "host suite product failure\n"
        self.assertFalse(
            parse_failing_names(log),
            "fixture must not parse case names so this is not the named-FAIL path",
        )
        self.assertFalse(
            collection_failed(log),
            "fixture must not match collection language",
        )
        for code in (4, 5):
            with self.subTest(exit=code):
                self.assertFalse(
                    collection_failed(log, code),
                    "exit %s without collection language must not be collection death"
                    % code,
                )
                cmd = (
                    "python3 -c \"print('host suite product failure'); "
                    "raise SystemExit(%d)\"" % code
                )
                self.assertNotIn("pytest", cmd)
                run = run_suite(repo, cmd, timeout=30)
                self.assertEqual(run.get("exit"), code, run)
                self.assertFalse(
                    parse_failing_names(run.get("output") or ""),
                    run,
                )
                self.assertFalse(run.get("collection_failed"), run)
                self.assertEqual(run.get("status"), "FAIL", run)
                self.assertNotEqual(run.get("status"), "UNVERIFIED", run)
                self.assertTrue(is_product_fail(run), run)
                self.assertTrue(is_product_fail(status=run.get("status")))
                self.assertTrue(needs_repair(run))

    def test_parse_failing_names_miss_does_not_imply_clean_fail_set(self):
        log = (
            "*** TEST FAILURE ***\n"
            "The host suite exited 1.\n"
            "No individual case names were printed by this runner.\n"
        )
        self.assertEqual(parse_failing_names(log), [])
        cmp = compare(
            {"status": "PASS", "failing": []},
            {
                "status": "FAIL",
                "exit": 1,
                "failing": parse_failing_names(log),
                "output": log,
            },
        )
        self.assertNotEqual(cmp["verdict"], "REGRESSION")
        self.assertEqual(cmp["verdict"], "FAIL")
        self.assertTrue(
            _unparsed_signal(cmp),
            "unparsed FAIL log must not look like a clean empty fail-set: %r" % cmp,
        )


def _unparsed_signal(cmp):
    if cmp.get("names_unparsed") or cmp.get("unparsed_failures") or cmp.get("failing_unparsed"):
        return True
    if cmp.get("unparsed") is True:
        return True
    if str(cmp.get("name_parse") or "").lower() in ("unparsed", "unknown", "miss"):
        return True
    new = cmp.get("new_failures") or []
    return any(isinstance(x, str) and "unparsed" in x.lower() for x in new)


def _discover(repo, hint="", test_root=None):
    kwargs = {}
    if test_root is not None:
        kwargs["test_root"] = test_root
    try:
        return discover_test_command(repo, hint, **kwargs)
    except TypeError as exc:
        raise AssertionError(
            "discover_test_command must accept configured test_root, got %s" % exc
        ) from exc


_REAL_IMPORT = __import__


def _block_pytest(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "pytest" or name.startswith("pytest."):
        raise ImportError("blocked so the unittest fallback is the evaluated path")
    return _REAL_IMPORT(name, globals, locals, fromlist, level)


def _without_pytest():
    """Force the unittest/empty discovery path even if pytest is already imported."""
    blocked = dict(sys.modules)
    blocked["pytest"] = None
    return mock.patch.dict(sys.modules, blocked), mock.patch(
        "builtins.__import__", _block_pytest
    )


def invents_selected_dir(cmd, test_root):
    """True when a discovered command names a filesystem path as the suite."""
    if not cmd:
        return False
    tokens = cmd.split()
    rel = (test_root or "").strip().rstrip("/")
    labeled = rel in ("", "tests")
    if rel and not labeled and rel in tokens:
        return True
    if "pytest" in tokens or any(t.endswith("pytest") for t in tokens):
        idx = next(
            (i for i, t in enumerate(tokens) if t == "pytest" or t.endswith("pytest")),
            -1,
        )
        args = [t for t in tokens[idx + 1 :] if not t.startswith("-")]
        if args:
            return True
    if "discover" in tokens and "-s" in tokens:
        start = tokens[tokens.index("-s") + 1] if tokens.index("-s") + 1 < len(tokens) else ""
        if not labeled:
            return True
        if start and start != "tests":
            return True
    return False


class DiscoverTestRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _nested(self):
        nested = self.repo / "pkg" / "nested" / "tests"
        nested.mkdir(parents=True)
        (nested / "test_foo.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_foo(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        return "pkg/nested/tests"

    def _assert_legal_nested_command(self, cmd, root):
        self.assertFalse(
            invents_selected_dir(cmd, root),
            "nested test_root must not invent a selected-dir suite, got %r" % cmd,
        )
        if cmd:
            self.assertNotIn(root, cmd.split(), cmd)
            self.assertNotIn("-s tests", cmd)
            allowed_manifests = (
                "make test",
                "npm test",
                "cargo test",
                "go test ./...",
                "python3 -m pytest -q",
            )
            self.assertTrue(
                cmd in allowed_manifests or cmd == "",
                "illegal nested discovery %r" % cmd,
            )

    def test_nested_test_root_without_manifest_is_discovered(self):
        # Name kept: the property is "does not invent a selected-dir suite",
        # not "status != UNVERIFIED after inventing -s <nested>".
        root = self._nested()
        cmd = _discover(self.repo, "", test_root=root)
        self._assert_legal_nested_command(cmd, root)
        mods, imp = _without_pytest()
        with mods, imp:
            blocked = _discover(self.repo, "", test_root=root)
        self._assert_legal_nested_command(blocked, root)
        if not cmd:
            run = run_suite(self.repo, cmd, timeout=30)
            self.assertEqual(run.get("status"), "UNVERIFIED", run)
            self.assertIn("no test command", (run.get("output") or "").lower())

    def test_nested_test_root_does_not_invent_selected_dir(self):
        root = self._nested()
        for blocked in (False, True):
            if blocked:
                mods, imp = _without_pytest()
                with mods, imp:
                    cmd = _discover(self.repo, "", test_root=root)
            else:
                cmd = _discover(self.repo, "", test_root=root)
            with self.subTest(pytest_blocked=blocked, cmd=cmd):
                self.assertFalse(invents_selected_dir(cmd, root), cmd)
                self.assertNotIn(root, (cmd or "").split())

    def test_configured_test_root_wins_over_hardcoded_tests_dir(self):
        root = self._nested()
        conventional = self.repo / "tests"
        conventional.mkdir()
        (conventional / "test_old.py").write_text("def test_old():\n    assert True\n", encoding="utf-8")
        mods, imp = _without_pytest()
        with mods, imp:
            cmd = _discover(self.repo, "", test_root=root)
        self._assert_legal_nested_command(cmd, root)
        self.assertNotIn("-s tests", cmd)
        self.assertNotIn("discover -s tests", cmd)
        cmd_pytest = _discover(self.repo, "", test_root=root)
        self._assert_legal_nested_command(cmd_pytest, root)

    def test_empty_test_root_keeps_tests_convention(self):
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "test_foo.py").write_text("def test_foo():\n    assert True\n", encoding="utf-8")
        cmd = _discover(self.repo, "", test_root="")
        self.assertTrue(cmd)
        self.assertTrue(
            cmd == "python3 -m pytest -q" or "unittest discover -s tests" in cmd,
            cmd,
        )
        self.assertFalse(invents_selected_dir(cmd, ""))

    def test_hint_and_repo_root_manifests_still_win(self):
        nested = self._nested()
        (self.repo / "pkg" / "nested" / "tests" / "Makefile").write_text(
            "test:\n\techo nested-makefile\n", encoding="utf-8"
        )
        self.assertEqual(_discover(self.repo, "true", test_root=nested), "true")

        (self.repo / "Makefile").write_text("test:\n\techo root\n", encoding="utf-8")
        self.assertEqual(_discover(self.repo, "", test_root=nested), "make test")
        (self.repo / "Makefile").unlink()

        (self.repo / "package.json").write_text(
            '{"scripts": {"test": "echo npm"}}\n', encoding="utf-8"
        )
        self.assertEqual(_discover(self.repo, "", test_root=nested), "npm test")
        (self.repo / "package.json").unlink()

        (self.repo / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
        self.assertEqual(_discover(self.repo, "", test_root=nested), "cargo test")
        (self.repo / "Cargo.toml").unlink()

        (self.repo / "go.mod").write_text("module x\n", encoding="utf-8")
        self.assertEqual(_discover(self.repo, "", test_root=nested), "go test ./...")

    def test_python_runner_manifest_uses_configured_test_root_path(self):
        # pytest.ini selects the python runner; it must not append the nested root.
        root = self._nested()
        (self.repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        cmd = _discover(self.repo, "", test_root=root)
        self._assert_legal_nested_command(cmd, root)
        if cmd:
            self.assertTrue(
                cmd == "python3 -m pytest -q" or cmd in ("make test", "npm test"),
                cmd,
            )
        mods, imp = _without_pytest()
        with mods, imp:
            blocked = _discover(self.repo, "", test_root=root)
        self._assert_legal_nested_command(blocked, root)

    def test_trailing_slash_test_root_is_the_tests_convention(self):
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "test_foo.py").write_text("def test_foo():\n    assert True\n", encoding="utf-8")
        cmd = _discover(self.repo, "", test_root="tests/")
        self.assertTrue(
            cmd == "python3 -m pytest -q" or "unittest discover -s tests" in cmd,
            cmd,
        )
        self.assertNotIn("tests/", cmd)

    def test_set_test_root_without_tests_does_not_silently_use_hardcoded_tests(self):
        empty = self.repo / "pkg" / "empty"
        empty.mkdir(parents=True)
        cmd = _discover(self.repo, "", test_root="pkg/empty")
        self.assertEqual(cmd, "")
        self.assertNotIn("discover -s tests", cmd)

    def test_pipeline_host_suite_uses_configured_test_root(self):
        os.environ["TEAM_HOME"] = str(Path(__file__).resolve().parents[1])
        from tests.support.repo import init_repo
        from team.config import load_config
        from team.pipeline import start_feature

        init_repo(self.repo)
        root = self._nested()
        cfg = load_config(
            self.repo,
            fake=True,
            force=True,
            test_root=root,
            test_command="",
        )
        pipe = start_feature(cfg, "discover brief", "discover-root")
        pipe.phase_baseline()
        run = pipe.state.baseline or {}
        cmd = run.get("command") or ""
        self._assert_legal_nested_command(cmd, root)
        if not cmd:
            self.assertEqual(run.get("status"), "UNVERIFIED", run)


if __name__ == "__main__":
    unittest.main()
