"""Runtime success means schema-valid output; write scopes apply to every runtime."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.config import PHASE_ORDER, ROLES, may_write, schema_path
from team.pipeline import OptionalPhaseError, PipelineError, start_feature
from team.runners import (
    FakeRuntime,
    RuntimeError_,
    _run,
    _write_tool_globs,
    claude_cmd,
    grok_cmd,
    unfinished_inspect,
    write_tool_path_filters,
)
from team.util import extract_json, load_json
from tests.support.claude_argv import (
    assert_claude_language,
    claude_allowed_write_roots,
    claude_flag_occurrences,
    claude_read_tools_enabled,
    claude_terminal_permitted,
    claude_tool_permitted,
    claude_session_id,
    claude_session_resumed,
    claude_write_denied,
)
from tests.support.grok_argv import (
    GrokArgvNotGrokLanguage,
    grok_flag_values,
    grok_read_tools_enabled,
    grok_search_replace_permitted,
    grok_session_id,
    grok_session_resumed,
    grok_write_denied,
    path_glob_matches,
)
from tests.support.hostile import HostileRuntime, emit, register_runtime
from tests.support.repo import init_repo


def _required_missing(obj, schema):
    if not isinstance(schema, dict):
        return []
    if schema.get("type") == "object":
        if not isinstance(obj, dict):
            return ["<not-object>"]
        return [key for key in schema.get("required") or [] if key not in obj]
    return []


class ExtractJsonContractTests(unittest.TestCase):
    def test_unparseable_payload_is_not_a_truthy_raw_dict(self):
        out = extract_json("usage: claude [options]\nunknown flag")
        if isinstance(out, dict) and "_raw" in out:
            self.fail("unparseable stdout must not become a truthy {_raw} dict")
        if out:
            self.fail("unparseable stdout must be a parse failure, got %r" % (out,))


class RunExitContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("hi\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _script(self, body: str) -> Path:
        path = self.root / "cli"
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def test_nonzero_exit_with_valid_json_is_failure(self):
        script = self._script('echo \'{"findings":[],"summary":"ok"}\'\nexit 2')
        result = _run(
            [str(script)],
            repo=self.root,
            timeout=5,
            session_id="s",
            prompt_path=self.prompt,
        )
        self.assertFalse(result.success)

    def test_unparseable_stdout_even_on_exit_zero_is_failure(self):
        script = self._script("printf '%s\\n' 'usage: claude [options]' 'unknown flag'\nexit 0")
        result = _run(
            [str(script)],
            repo=self.root,
            timeout=5,
            session_id="s",
            prompt_path=self.prompt,
        )
        self.assertFalse(result.success)
        self.assertIn("parse", (result.error or "").lower())

    def test_valid_json_on_exit_zero_without_schema_still_parses(self):
        script = self._script('echo \'{"findings":[],"summary":"ok"}\'\nexit 0')
        result = _run(
            [str(script)],
            repo=self.root,
            timeout=5,
            session_id="s",
            prompt_path=self.prompt,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.output.get("summary"), "ok")

    def test_grok_envelope_with_nested_findings_unwraps_to_review_schema(self):
        review = {
            "summary": "ok",
            "findings": [
                {
                    "severity": "low",
                    "title": "F82 residual",
                    "evidence": "x",
                    "kind": "note",
                }
            ],
        }
        placeholder = {"summary": "reading", "findings": []}
        envelope = {
            "text": json.dumps(placeholder) + json.dumps(review) + "<|eos|>",
            "sessionId": "sid-from-grok",
            "stopReason": "end_turn",
        }
        payload = self.root / "stdout.json"
        payload.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
        script = self._script("cat '%s'\nexit 0" % payload)
        schema = load_json(schema_path("review.json"))
        result = _run(
            [str(script)],
            repo=self.root,
            timeout=5,
            session_id="s",
            prompt_path=self.prompt,
            schema=schema,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.output.get("summary"), "ok")
        self.assertEqual(len(result.output.get("findings") or []), 1)
        self.assertEqual(result.output["findings"][0]["title"], "F82 residual")
        self.assertEqual(result.num_turns, None)
        # Missing turns is reject for inspect roles. The predicate takes no
        # runtime: the rule is about tool-loop evidence, and a vendor gate would
        # accept the same non-review from any other adapter.
        for role in ("reviewer", "guardian", "scout", "critic"):
            self.assertTrue(
                unfinished_inspect(role=role, num_turns=result.num_turns, output=result.output),
                "%s with num_turns=None must be unfinished" % role,
            )
        self.assertFalse(
            unfinished_inspect(role="implementer", num_turns=result.num_turns, output=result.output)
        )

    def test_grok_envelope_num_turns_is_preserved(self):
        envelope = {
            "text": json.dumps({"summary": "ok", "findings": []}),
            "sessionId": "sid-from-grok",
            "num_turns": 1,
            "stopReason": "end_turn",
        }
        payload = self.root / "stdout.json"
        payload.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
        script = self._script("cat '%s'\nexit 0" % payload)
        result = _run(
            [str(script)],
            repo=self.root,
            timeout=5,
            session_id="s",
            prompt_path=self.prompt,
            schema=load_json(schema_path("review.json")),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.num_turns, 1)
        self.assertTrue(
            unfinished_inspect(role="reviewer", num_turns=result.num_turns, output=result.output)
        )
        self.assertTrue(
            unfinished_inspect(role="guardian", num_turns=result.num_turns, output=result.output)
        )
        self.assertFalse(
            unfinished_inspect(role="implementer", num_turns=result.num_turns, output=result.output)
        )


class InvokeSchemaContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_invalid_reviewer_output_is_pipeline_error(self):
        from team.config import load_config

        cfg = load_config(self.repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_feature(cfg, "brief", "schema-inv")
        hostile = HostileRuntime(
            [emit({"summary": "ok"})],
            phases=("reviewer-fake",),
        )
        before = (pipe.work / "review.md").read_text(encoding="utf-8") if (pipe.work / "review.md").is_file() else None
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_reviewer()
        self.assertIn("findings", str(ctx.exception).lower())
        after = pipe.work / "review.md"
        if after.is_file() and before is None:
            self.fail("failed reviewer must not write review.md")
        result_path = pipe.work / "prompts" / "reviewer-fake.result.json"
        self.assertTrue(result_path.is_file())
        body = result_path.read_text(encoding="utf-8")
        self.assertTrue(body.strip())

    def test_one_turn_grok_review_is_rejected_after_retry(self):
        from team.config import load_config

        cfg = load_config(self.repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_feature(cfg, "brief", "one-turn-rev")
        hostile = HostileRuntime(
            [
                emit(
                    {
                        "summary": "Reviewing the collected range first.",
                        "findings": [
                            {
                                "severity": "low",
                                "title": "Review in progress",
                                "evidence": "Starting with the orchestrator artifacts.",
                                "kind": "note",
                            }
                        ],
                    }
                )
            ],
            phases=("reviewer-grok",),
            num_turns=1,
        )
        with register_runtime("grok", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.invoke(
                    "reviewer",
                    "reviewer-grok",
                    "inspect the tree",
                    "review.json",
                    runtime_name="grok",
                )
        self.assertIn("without inspecting", str(ctx.exception).lower())
        reviewer_calls = [c for c in hostile.calls if c["phase"] == "reviewer-grok"]
        self.assertEqual(len(reviewer_calls), 2)

    def test_missing_num_turns_is_rejected_for_inspect_roles(self):
        from team.config import load_config

        progress = {
            "summary": "Reviewing the collected range first.",
            "findings": [
                {
                    "severity": "low",
                    "title": "Review in progress",
                    "evidence": "Starting with the orchestrator artifacts.",
                    "kind": "note",
                }
            ],
        }
        cfg = load_config(self.repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_feature(cfg, "brief", "missing-turns")
        hostile = HostileRuntime(
            [emit(progress)],
            phases=("reviewer-fake",),
            num_turns=None,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.invoke(
                    "reviewer",
                    "reviewer-fake",
                    "inspect the tree",
                    "review.json",
                    runtime_name="fake",
                )
        self.assertIn("without inspecting", str(ctx.exception).lower())
        self.assertGreaterEqual(
            len([c for c in hostile.calls if c["phase"] == "reviewer-fake"]), 2
        )

        guardian_progress = {
            "risks": [],
            "chain": {
                "r_to_a": {"ok": True, "note": "n"},
                "a_to_t": {"ok": True, "note": "n"},
                "t_to_i": {"ok": True, "note": "n"},
                "i_to_r": {"ok": True, "note": "n"},
            },
            "guardian_markdown": "still reading",
        }
        g_hostile = HostileRuntime(
            [emit(guardian_progress)],
            phases=("guardian",),
            num_turns=None,
        )
        with register_runtime("fake", g_hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.invoke(
                    "guardian",
                    "guardian",
                    "inspect the tree",
                    "guardian.json",
                    runtime_name="fake",
                )
        self.assertIn("inspect", str(ctx.exception).lower())

        finished = {
            "summary": "Range is complete; findings below.",
            "findings": [
                {
                    "severity": "low",
                    "title": "Residual note on docs",
                    "evidence": "README line 1",
                    "kind": "note",
                }
            ],
            "review_markdown": "Finished review of the collected range.",
        }
        finished_rt = HostileRuntime(
            [emit(finished)],
            phases=("reviewer-fake",),
            num_turns=None,
        )
        with register_runtime("fake", finished_rt):
            with self.assertRaises(PipelineError) as ctx:
                pipe.invoke(
                    "reviewer",
                    "reviewer-fake",
                    "inspect the tree",
                    "review.json",
                    runtime_name="fake",
                )
        self.assertIn("inspect", str(ctx.exception).lower())
        self.assertGreaterEqual(
            len([c for c in finished_rt.calls if c["phase"] == "reviewer-fake"]),
            2,
        )

    def test_inspect_guard_is_role_not_grok_hardcode(self):
        from team.config import load_config

        progress = {
            "summary": "Reviewing the collected range first.",
            "findings": [
                {
                    "severity": "low",
                    "title": "Review in progress",
                    "evidence": "Starting with the orchestrator artifacts.",
                    "kind": "note",
                }
            ],
        }
        cfg = load_config(self.repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_feature(cfg, "brief", "role-not-grok")
        for runtime_name, phase in (("fake", "reviewer-fake"), ("claude", "reviewer-claude")):
            hostile = HostileRuntime(
                [emit(progress)],
                phases=(phase,),
                num_turns=1,
            )
            with register_runtime(runtime_name, hostile):
                with self.assertRaises(PipelineError) as ctx:
                    pipe.invoke(
                        "reviewer",
                        phase,
                        "inspect the tree",
                        "review.json",
                        runtime_name=runtime_name,
                    )
            self.assertIn("without inspecting", str(ctx.exception).lower())

    def test_failed_reviewer_does_not_look_like_a_clean_review(self):
        from team.config import load_config

        cfg = load_config(self.repo, fake=True, force=True)
        pipe = start_feature(cfg, "brief", "crash-rev")
        hostile = HostileRuntime(
            [emit({"_raw": "usage: claude [options]"})],
            phases=("reviewer-fake",),
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError):
                pipe.phase_reviewer()
        review = pipe.work / "review.md"
        if review.is_file():
            text = review.read_text(encoding="utf-8")
            self.assertNotIn("Fake review", text)

    def test_drafting_after_inspect_is_not_retried(self):
        """A 32-turn stub is not a review. Retry would re-read the dump."""
        from team.config import load_config

        draft = {
            "summary": "Highest-severity issues are below.",
            "findings": [],
            "review_markdown": "drafting",
        }
        cfg = load_config(self.repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_feature(cfg, "brief", "draft-rev")
        hostile = HostileRuntime(
            [emit(draft)],
            phases=("reviewer-fake",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.invoke(
                    "reviewer",
                    "reviewer-fake",
                    "inspect the tree",
                    "review.json",
                    runtime_name="fake",
                )
        self.assertIn("progress note", str(ctx.exception).lower())
        self.assertEqual(
            len([c for c in hostile.calls if c["phase"] == "reviewer-fake"]),
            1,
        )

    def test_empty_findings_with_real_markdown_is_a_finished_review(self):
        from team.config import load_config

        clean = {
            "summary": "Range is clean.",
            "findings": [],
            "review_markdown": "Inspected the collected commits. No findings.",
            "census_markdown": "# Census\n\nlayout\n",
        }
        cfg = load_config(self.repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_feature(cfg, "brief", "clean-rev")
        hostile = HostileRuntime(
            [emit(clean)],
            phases=("reviewer-fake",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            result = pipe.invoke(
                "reviewer",
                "reviewer-fake",
                "inspect the tree",
                "review.json",
                runtime_name="fake",
            )
        self.assertTrue(result.success)
        self.assertEqual(result.output.get("findings"), [])

    def test_one_turn_implementer_with_no_delta_is_rejected(self):
        from team.config import load_config

        stub = {"summary": "Reading census, brief, review, and apply-plan.", "paths_touched": []}
        cfg = load_config(self.repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_feature(cfg, "brief", "stub-impl")
        (self.repo / "src").mkdir(exist_ok=True)
        (self.repo / "src" / "keep.py").write_text("keep\n", encoding="utf-8")
        hostile = HostileRuntime(
            [emit(stub)],
            phases=("implementer-apply",),
            num_turns=1,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.invoke(
                    "implementer",
                    "implementer-apply",
                    "patch",
                    "write_summary.json",
                    capability="write-code",
                    runtime_name="fake",
                )
        self.assertIn("no product delta", str(ctx.exception).lower())
        self.assertGreaterEqual(
            len([c for c in hostile.calls if c["phase"] == "implementer-apply"]),
            2,
        )

    def test_one_turn_debugger_is_rejected_after_retry(self):
        from team.config import load_config

        stub = {
            "owner": "unknown",
            "diagnosis_markdown": (
                "Starting read-only diagnosis: loading census, contract, "
                "baseline, and apply reports."
            ),
        }
        cfg = load_config(self.repo, fake=True, force=True, code_root="src", test_root="tests")
        pipe = start_feature(cfg, "brief", "draft-dbg")
        pipe.state.final = {"status": "FAIL", "exit": 1}
        hostile = HostileRuntime(
            [emit(stub)],
            phases=("debugger",),
            num_turns=1,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises((PipelineError, OptionalPhaseError)) as ctx:
                pipe.phase_debugger()
        self.assertIn("inspect", str(ctx.exception).lower())
        self.assertEqual(
            len([c for c in hostile.calls if c["phase"] == "debugger"]),
            2,
        )


class GrokArgvInterpreterTests(unittest.TestCase):
    """The double is local. It must reject Claude spelling and honor Grok flags."""

    def test_claude_edit_glob_is_not_grok_language(self):
        argv = [
            "grok",
            "--always-approve",
            "--allow",
            "Edit(tests/**)",
            "--deny",
            "Write(src/team/**)",
        ]
        with self.assertRaises(GrokArgvNotGrokLanguage):
            grok_search_replace_permitted(argv, "tests/foo.py")

    def test_path_globs_scope_search_replace(self):
        argv = [
            "grok",
            "--tools",
            "search_replace",
            "--allow",
            "tests/**",
            "--deny",
            "src/team/**",
        ]
        self.assertTrue(grok_search_replace_permitted(argv, "tests/foo.py"))
        self.assertFalse(grok_search_replace_permitted(argv, "src/team/x.py"))
        self.assertTrue(path_glob_matches("tests/**", "tests/foo.py"))
        self.assertFalse(path_glob_matches("tests", "src/team/x.py"))

    def test_disallowed_tools_search_replace_denies_every_path(self):
        argv = [
            "grok",
            "--tools",
            "read_file,grep,list_dir",
            "--disallowed-tools",
            "search_replace",
        ]
        self.assertFalse(grok_search_replace_permitted(argv, "tests/foo.py"))
        self.assertFalse(grok_search_replace_permitted(argv, "src/team/x.py"))

    def test_dot_deny_roots_without_whole_repo_allow(self):
        argv = [
            "grok",
            "--tools",
            "search_replace",
            "--deny",
            "inferedge-phase1/tests/**",
            "--deny",
            "appliance-console/**",
        ]
        self.assertTrue(grok_search_replace_permitted(argv, "top.py"))
        self.assertFalse(grok_search_replace_permitted(argv, "inferedge-phase1/tests/t.py"))
        self.assertFalse(grok_search_replace_permitted(argv, "appliance-console/page.tsx"))

    def test_write_only_tools_list_does_not_enable_read(self):
        argv = ["grok", "--tools", "search_replace", "--allow", "tests/**"]
        self.assertFalse(grok_read_tools_enabled(argv))
        self.assertTrue(grok_search_replace_permitted(argv, "tests/foo.py"))

    def test_read_plus_search_replace_enables_both(self):
        argv = [
            "grok",
            "--tools",
            "read_file,grep,list_dir,search_replace",
            "--allow",
            "tests/**",
        ]
        self.assertTrue(grok_read_tools_enabled(argv))
        self.assertTrue(grok_search_replace_permitted(argv, "tests/foo.py"))

    def test_disallowed_read_tool_fails_closed(self):
        argv = [
            "grok",
            "--tools",
            "read_file,grep,list_dir,search_replace",
            "--disallowed-tools",
            "read_file",
        ]
        self.assertFalse(grok_read_tools_enabled(argv))


class PathScopeTests(unittest.TestCase):
    def test_claude_write_tests_and_write_code_are_not_identical(self):
        extra = {"test_root": "tests", "code_root": "src/team"}
        tests_cmd = claude_cmd(
            prompt="hi",
            schema=None,
            capability="write-tests",
            session_id="s",
            resume=False,
            extra=extra,
        )
        code_cmd = claude_cmd(
            prompt="hi",
            schema=None,
            capability="write-code",
            session_id="s",
            resume=False,
            extra=extra,
        )
        self.assertNotEqual(tests_cmd, code_cmd)
        tests_s = " ".join(str(x) for x in tests_cmd)
        code_s = " ".join(str(x) for x in code_cmd)
        self.assertTrue(
            "tests" in tests_s or "--settings" in tests_s,
            "write-tests must path-scope the test root: %s" % tests_s,
        )
        self.assertTrue(
            "src/team" in code_s or "--settings" in code_s,
            "write-code must path-scope the code root: %s" % code_s,
        )

    def _grok(self, capability, extra):
        return grok_cmd(
            prompt_path=Path("/tmp/p.md"),
            schema=None,
            capability=capability,
            session_id="s",
            resume=False,
            repo=Path("/tmp/repo"),
            extra=extra,
        )

    def _assert_search_replace(self, cmd, rel, allowed):
        try:
            got = grok_search_replace_permitted(cmd, rel)
        except GrokArgvNotGrokLanguage as exc:
            self.fail(
                "grok_cmd is not Grok --tools/--allow/--deny language: %s\nargv=%s"
                % (exc, cmd)
            )
        self.assertEqual(
            got,
            allowed,
            "search_replace %s on %s; argv=%s" % ("allowed" if allowed else "denied", rel, cmd),
        )

    def test_grok_write_capabilities_remain_path_scoped(self):
        extra = {"test_root": "tests", "code_root": "src/team"}
        tests_cmd = self._grok("write-tests", extra)
        code_cmd = self._grok("write-code", extra)
        tests_s = " ".join(str(x) for x in tests_cmd)
        code_s = " ".join(str(x) for x in code_cmd)
        self.assertNotIn("Edit(", tests_s)
        self.assertNotIn("Write(", tests_s)
        self.assertNotIn("Edit(", code_s)
        self.assertNotIn("Write(", code_s)
        self.assertTrue(
            "--allow" in tests_cmd
            or "--deny" in tests_cmd
            or "--tools" in tests_cmd
            or "--disallowed-tools" in tests_cmd,
            "write-tests must use Grok flags: %s" % tests_s,
        )
        self.assertIn("search_replace", tests_s)
        self.assertIn("search_replace", code_s)
        self.assertNotEqual(tests_cmd, code_cmd)
        self._assert_search_replace(tests_cmd, "tests/a.py", True)
        self._assert_search_replace(tests_cmd, "src/team/a.py", False)
        self._assert_search_replace(code_cmd, "src/team/a.py", True)
        self._assert_search_replace(code_cmd, "tests/a.py", False)
        self.assertTrue(
            grok_read_tools_enabled(tests_cmd),
            "write-tests --tools must keep read_file,grep,list_dir; got %s" % tests_s,
        )
        self.assertTrue(
            grok_read_tools_enabled(code_cmd),
            "write-code --tools must keep read_file,grep,list_dir; got %s" % code_s,
        )

    def test_grok_write_code_dot_denies_test_root_and_submodules_as_path_globs(self):
        extra = {
            "code_root": ".",
            "test_root": "inferedge-phase1/tests",
            "submodule_paths": ["appliance-console", "appliance-support"],
        }
        allow, deny = write_tool_path_filters("write-code", extra)
        self.assertEqual(allow, [])
        self.assertEqual(
            deny,
            [
                "inferedge-phase1/tests",
                "appliance-console",
                "appliance-support",
            ],
        )
        self.assertNotIn(".", deny)
        cmd = self._grok("write-code", extra)
        joined = " ".join(str(x) for x in cmd)
        self.assertNotIn("Edit(", joined)
        self.assertNotIn("Write(", joined)
        self.assertNotIn("Edit(./**)", joined)
        self.assertNotIn("./**", grok_flag_values(cmd, "--allow"))
        deny_vals = grok_flag_values(cmd, "--deny")
        self.assertTrue(deny_vals, "write-code code_root='.' must pass --deny path globs")
        for root in (
            "inferedge-phase1/tests",
            "appliance-console",
            "appliance-support",
        ):
            self.assertTrue(
                any(
                    v == root or v == root + "/**" or v.startswith(root + "/")
                    for v in deny_vals
                ),
                "deny path globs must name %s (no Edit/Write wrappers), got %s"
                % (root, deny_vals),
            )
        self._assert_search_replace(cmd, "top.py", True)
        self._assert_search_replace(cmd, "inferedge-phase1/tests/t.py", False)
        self._assert_search_replace(cmd, "appliance-console/page.tsx", False)
        self._assert_search_replace(cmd, "appliance-support/lib.rs", False)

    def test_grok_write_allow_deny_are_path_globs_that_scope_search_replace(self):
        cases = [
            ({"test_root": "tests/", "code_root": "src"}, "tests", "src"),
            (
                {"test_root": "inferedge-phase1/tests", "code_root": "src"},
                "inferedge-phase1/tests",
                "src",
            ),
            ({"test_root": "tests", "code_root": "src/team"}, "tests", "src/team"),
        ]
        for extra, test_root, code_root in cases:
            with self.subTest(extra=extra):
                for cap in ("write-tests", "write-code"):
                    cmd = self._grok(cap, extra)
                    joined = " ".join(str(x) for x in cmd)
                    self.assertNotIn("Edit(", joined)
                    self.assertNotIn("Write(", joined)
                    self.assertIn("search_replace", joined)
                    for flag in ("--allow", "--deny"):
                        for value in grok_flag_values(cmd, flag):
                            self.assertFalse(
                                value.startswith("Edit(") or value.startswith("Write("),
                                "%s value must be a path glob, got %r" % (flag, value),
                            )
                            self.assertNotIn("//", value, "double-slash glob: %r" % value)
                    if extra["test_root"].endswith("/"):
                        for value in grok_flag_values(cmd, "--allow") + grok_flag_values(
                            cmd, "--deny"
                        ):
                            self.assertNotEqual(value, "tests/")
                            self.assertFalse(value.startswith("tests//"))
                    in_tests = test_root.rstrip("/") + "/foo.py"
                    in_code = code_root.rstrip("/") + "/a.py"
                    self._assert_search_replace(
                        cmd, in_tests, cap == "write-tests"
                    )
                    self._assert_search_replace(
                        cmd, in_code, cap == "write-code"
                    )
                self.assertNotEqual(
                    self._grok("write-tests", extra),
                    self._grok("write-code", extra),
                )
        ro = self._grok("read-only", {"test_root": "tests", "code_root": "src"})
        ro_s = " ".join(str(x) for x in ro)
        self.assertIn("--disallowed-tools", ro)
        self.assertIn("search_replace", ro_s)
        self.assertEqual(grok_flag_values(ro, "--allow"), [])
        self._assert_search_replace(ro, "tests/foo.py", False)
        self._assert_search_replace(ro, "src/a.py", False)
        self.assertTrue(grok_read_tools_enabled(ro), ro_s)

    def test_every_write_capability_keeps_read_tools(self):
        """Converse of path-scoped search_replace: the hop can still read."""
        extra = {"test_root": "tests", "code_root": "src"}
        for cap in ("read-only", "execute", "write-tests", "write-code"):
            cmd = self._grok(cap, extra)
            joined = " ".join(str(x) for x in cmd)
            self.assertTrue(
                grok_read_tools_enabled(cmd),
                "%s must enable read tools; argv=%s" % (cap, joined),
            )
            tools = ",".join(grok_flag_values(cmd, "--tools"))
            self.assertIn("read_file", tools, cap)
            if may_write(cap):
                self.assertIn("search_replace", tools, cap)
                self.assertNotIn("run_terminal_cmd", tools, cap)

    def test_every_non_write_role_denies_write_tools(self):
        """Personas without write-tests/write-code cannot Edit/search_replace."""
        extra = {"test_root": "tests", "code_root": "src"}
        seen = set()
        for role, spec in ROLES.items():
            cap = spec["capability"]
            if may_write(cap) or cap in seen:
                continue
            seen.add(cap)
            grok = self._grok(cap, extra)
            claude = claude_cmd(
                prompt="hi",
                schema=None,
                capability=cap,
                session_id="s",
                resume=False,
                extra=extra,
            )
            self._assert_search_replace(grok, "src/a.py", False)
            self._assert_search_replace(grok, "tests/a.py", False)
            self.assertIn("search_replace", grok_flag_values(grok, "--disallowed-tools"))
            disallowed = set()
            items = [str(x) for x in claude]
            for i, tok in enumerate(items):
                if tok == "--disallowedTools" and i + 1 < len(items):
                    disallowed.update(items[i + 1].split(","))
            self.assertTrue(
                {"Edit", "Write"}.issubset(disallowed),
                "%s/%s Claude argv must disallow Edit/Write; got %s"
                % (role, cap, disallowed),
            )

    def test_unknown_capability_is_non_write(self):
        extra = {"test_root": "tests", "code_root": "src"}
        grok = self._grok("future-inspect", extra)
        claude = claude_cmd(
            prompt="hi",
            schema=None,
            capability="future-inspect",
            session_id="s",
            resume=False,
            extra=extra,
        )
        self._assert_search_replace(grok, "src/a.py", False)
        joined = " ".join(str(x) for x in claude)
        self.assertIn("Edit", joined)
        self.assertIn("--disallowedTools", claude)

    def test_grok_semantic_double_cannot_search_replace_denied_root(self):
        extra = {"test_root": "tests", "code_root": "src/team"}
        tests_cmd = self._grok("write-tests", extra)
        code_cmd = self._grok("write-code", extra)
        dot_cmd = self._grok(
            "write-code",
            {
                "code_root": ".",
                "test_root": "inferedge-phase1/tests",
                "submodule_paths": ["appliance-console", "appliance-support"],
            },
        )
        self._assert_search_replace(tests_cmd, "tests/foo.py", True)
        self._assert_search_replace(tests_cmd, "src/team/x.py", False)
        self._assert_search_replace(code_cmd, "src/team/x.py", True)
        self._assert_search_replace(code_cmd, "tests/foo.py", False)
        self._assert_search_replace(dot_cmd, "top.py", True)
        self._assert_search_replace(dot_cmd, "inferedge-phase1/tests/t.py", False)
        self._assert_search_replace(dot_cmd, "appliance-console/page.tsx", False)

    def test_trailing_slash_test_root_is_one_glob(self):
        extra = {"code_root": ".", "test_root": "tests/"}
        allow, deny = write_tool_path_filters("write-code", extra)
        self.assertEqual(allow, [])
        self.assertEqual(deny, ["tests"])
        globs = _write_tool_globs(deny)
        self.assertIn("Edit(tests/**)", globs)
        self.assertIn("Write(tests/**)", globs)
        self.assertFalse(any("//" in g for g in globs), globs)

    def test_write_tool_globs_normalizes_raw_slash(self):
        """The glob builder is the last seam — callers may still pass tests/."""
        globs = _write_tool_globs(["tests/", "./src/"])
        self.assertEqual(
            globs,
            [
                "Edit(tests/**)",
                "Write(tests/**)",
                "Edit(src/**)",
                "Write(src/**)",
            ],
        )
        self.assertFalse(any("//" in g for g in globs), globs)
        self.assertEqual(_write_tool_globs(["."]), [])


class AdapterCapabilityParityTests(unittest.TestCase):
    """Two adapters, one capability model. The seam is claude_cmd ↔ grok_cmd.

    "Map the same capabilities" was a sentence in AGENTS.md that nothing
    checked, and the two argv builders drifted: Grok scoped both writers and
    withheld the terminal from a write hop, Claude scoped neither. The git
    fence still failed the hop afterwards, so this was never an escape --
    but the Claude path failed *after* the writes had landed where the Grok
    path prevented them.

    Each question is asked of both adapters through their own semantic
    double, and the answers must agree.
    """

    EXTRA = {
        "test_root": "tests",
        "code_root": "src/team",
        "submodule_paths": ["vendor/console"],
    }
    DOT_EXTRA = {
        "test_root": "tests",
        "code_root": ".",
        "submodule_paths": ["vendor/console"],
    }
    CAPABILITIES = ("read-only", "write-tests", "write-code", "execute", "future-thing")
    PATHS = ("tests/a.py", "src/team/a.py", "README.md", "vendor/console/x.ts")

    def _pair(self, capability, extra):
        claude = claude_cmd(
            prompt="hi",
            schema=None,
            capability=capability,
            session_id="s",
            resume=False,
            extra=extra,
        )
        grok = grok_cmd(
            prompt_path=Path("/tmp/p.md"),
            schema=None,
            capability=capability,
            session_id="s",
            resume=False,
            repo=Path("/tmp/repo"),
            extra=extra,
        )
        return claude, grok

    def test_denied_write_roots_are_the_same_on_every_adapter(self):
        """Refusal is the half of the two filter languages that must match."""
        for extra in (self.EXTRA, self.DOT_EXTRA):
            for capability in self.CAPABILITIES:
                claude, grok = self._pair(capability, extra)
                for rel in self.PATHS:
                    with self.subTest(capability=capability, rel=rel, root=extra["code_root"]):
                        self.assertEqual(
                            claude_write_denied(claude, rel),
                            grok_write_denied(grok, rel),
                            "adapters disagree on refusing %s under %s\nclaude=%s\ngrok=%s"
                            % (rel, capability, claude, grok),
                        )

    def test_claude_allow_is_pre_approval_and_the_fence_is_the_boundary(self):
        """Recorded residual, not an approved design.

        Grok's --allow is a real allowlist: write-tests cannot touch a path
        outside test_root. Claude has no equivalent under acceptEdits -- an
        Edit(tests/**) entry pre-approves, it does not refuse the rest -- so
        a path in neither root set is reachable on one adapter and not the
        other, and only the git write fence (_verify_write) catches it.

        Premise, in the words that would make this test wrong: if the Claude
        CLI ever gains an allowlist mode that fails closed for edits, this
        asymmetry should be closed in argv and this test deleted.
        """
        claude, grok = self._pair("write-tests", self.EXTRA)
        self.assertEqual(claude_allowed_write_roots(claude), ["tests/**"])
        self.assertFalse(grok_search_replace_permitted(grok, "README.md"))
        self.assertFalse(claude_write_denied(claude, "README.md"))

    def test_no_capability_lets_one_adapter_run_a_terminal_and_not_the_other(self):
        for capability in self.CAPABILITIES:
            claude, grok = self._pair(capability, self.EXTRA)
            tools = grok_flag_values(grok, "--tools")
            grok_terminal = bool(tools) and "run_terminal_cmd" in tools[0].split(",")
            with self.subTest(capability=capability):
                self.assertEqual(
                    claude_terminal_permitted(claude),
                    grok_terminal,
                    "terminal differs on %s: claude=%s grok=%s" % (capability, claude, grok),
                )
        # And it is only the execute capability that has one at all.
        claude, _grok = self._pair("execute", self.EXTRA)
        self.assertTrue(claude_terminal_permitted(claude))
        claude, _grok = self._pair("write-code", self.EXTRA)
        self.assertFalse(claude_terminal_permitted(claude))

    def test_every_capability_keeps_read_tools_on_both_adapters(self):
        for capability in self.CAPABILITIES:
            claude, grok = self._pair(capability, self.EXTRA)
            with self.subTest(capability=capability):
                self.assertTrue(claude_read_tools_enabled(claude), claude)
                self.assertTrue(grok_read_tools_enabled(grok), grok)

    def test_claude_tool_filter_flags_appear_once(self):
        """Repeated occurrences leave union-vs-last-wins to the CLI.

        A last-wins CLI would silently narrow the scope to the final root.
        """
        for capability in self.CAPABILITIES:
            for extra in (self.EXTRA, self.DOT_EXTRA):
                claude, _grok = self._pair(capability, extra)
                for flag in ("--allowedTools", "--disallowedTools"):
                    with self.subTest(capability=capability, flag=flag):
                        self.assertEqual(claude_flag_occurrences(claude, flag), 1, claude)

    def test_a_path_that_cannot_be_a_claude_filter_fails_loudly(self):
        with self.assertRaises(RuntimeError_):
            claude_cmd(
                prompt="hi",
                schema=None,
                capability="write-tests",
                session_id="s",
                resume=False,
                extra={"test_root": "te,sts", "code_root": "src"},
            )

    def test_no_write_capability_lets_an_unscoped_writer_through(self):
        claude, _grok = self._pair("write-tests", self.EXTRA)
        self.assertFalse(claude_tool_permitted(claude, "NotebookEdit"), claude)


class FakeOutputSchemaSeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name) / "work"
        (self.work / "prompts").mkdir(parents=True)
        self.repo = Path(self.tmp.name)
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def _pipeline_src(self) -> str:
        return (ROOT / "src" / "team" / "pipeline.py").read_text(encoding="utf-8")

    def test_every_schema_file_is_referenced_from_pipeline(self):
        schema_dir = ROOT / "schemas"
        on_disk = {p.name for p in schema_dir.glob("*.json")}
        referenced = set(
            re.findall(r'\.invoke\([^)]*"([a-z0-9_]+\.json)"', self._pipeline_src(), re.S)
        )
        missing = on_disk - referenced
        extra = referenced - on_disk
        self.assertFalse(missing, "schema files never passed to invoke: %s" % sorted(missing))
        self.assertFalse(extra, "invoke names a schema that is not on disk: %s" % sorted(extra))

    def test_fake_canned_output_satisfies_required_keys(self):
        src = self._pipeline_src()
        pairs = re.findall(
            r'invoke\(\s*"[^"]+"\s*,\s*"([^"%]+)"\s*,\s*[^,]+,\s*"([a-z0-9_]+\.json)"',
            src,
        )
        pairs += re.findall(
            r'invoke\(\s*\n\s*"[^"]+"\s*,\s*\n\s*"([^"%]+)"\s*,\s*\n\s*[^,]+,\s*\n\s*"([a-z0-9_]+\.json)"',
            src,
        )
        if "review.json" in src:
            pairs.append(("reviewer-fake", "review.json"))
        if "answers.json" in src:
            pairs.append(("consult-001", "answers.json"))
        self.assertTrue(pairs, "could not derive invoke(phase, schema) pairs from pipeline.py")
        rt = FakeRuntime()
        extra = {"code_root": "src", "test_root": "tests"}
        seen = set()
        for phase, schema_name in pairs:
            key = (phase, schema_name)
            if key in seen:
                continue
            seen.add(key)
            result = rt.complete(
                role="architect",
                phase=phase,
                prompt="x",
                schema=None,
                capability="read-only",
                work=self.work,
                repo=self.repo,
                extra=extra,
            )
            schema = load_json(schema_path(schema_name))
            missing = _required_missing(result.output, schema)
            self.assertEqual(
                missing,
                [],
                "FakeRuntime phase %s vs %s missing %s (keys=%s)"
                % (phase, schema_name, missing, sorted(result.output)),
            )

    def test_phase_order_write_roles_have_a_schema(self):
        src = self._pipeline_src()
        for phase in PHASE_ORDER:
            if phase in ("baseline", "final-test", "adversarial-test", "verify-test"):
                continue
            if phase == "repair":
                continue
            self.assertTrue(
                re.search(r'"%s"' % re.escape(phase), src)
                or phase == "reviewer"
                and "reviewer-%s" in src,
                "PHASE_ORDER entry %s has no invoke site" % phase,
            )


if __name__ == "__main__":
    unittest.main()


class WarmSessionParityTests(unittest.TestCase):
    """Resume is one question asked of both adapters.

    A warm chain is an accelerator, so the two CLIs must agree on when a hop
    continues a thread and when it opens one -- otherwise "warm" would mean
    something different depending on which runtime a role happens to hold.
    """

    def _pair(self, *, resume):
        claude = claude_cmd(
            prompt="hi",
            schema=None,
            capability="read-only",
            session_id="sid-1",
            resume=resume,
        )
        grok = grok_cmd(
            prompt_path=Path("/tmp/p.md"),
            schema=None,
            capability="read-only",
            session_id="sid-1",
            resume=resume,
            repo=Path("/tmp/r"),
        )
        return claude, grok

    def test_cold_opens_a_session_on_both(self):
        claude, grok = self._pair(resume=False)
        self.assertFalse(claude_session_resumed(claude))
        self.assertFalse(grok_session_resumed(grok))
        self.assertEqual(claude_session_id(claude), "sid-1")
        self.assertEqual(grok_session_id(grok), "sid-1")

    def test_warm_continues_the_same_session_on_both(self):
        claude, grok = self._pair(resume=True)
        self.assertTrue(claude_session_resumed(claude))
        self.assertTrue(grok_session_resumed(grok))
        self.assertEqual(
            claude_session_id(claude),
            grok_session_id(grok),
            "both adapters continue the id they were handed",
        )

    def test_resume_without_an_id_is_still_cold(self):
        """Nothing to continue is not a reason to drop the flag entirely."""
        claude = claude_cmd(
            prompt="hi",
            schema=None,
            capability="read-only",
            session_id="",
            resume=True,
        )
        self.assertFalse(claude_session_resumed(claude))

    def test_headless_survives_a_warm_hop(self):
        """Resuming must not cost either CLI its headless spelling."""
        claude, grok = self._pair(resume=True)
        assert_claude_language(claude)
        flat = [str(a) for a in grok]
        self.assertIn("--no-alt-screen", flat)
        self.assertIn("--prompt-file", flat)
        self.assertNotIn("--fullscreen", flat)
