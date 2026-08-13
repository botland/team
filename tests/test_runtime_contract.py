"""Runtime success means schema-valid output; write scopes apply to every runtime."""

from __future__ import annotations

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

from team.config import PHASE_ORDER, schema_path
from team.pipeline import PipelineError, start_feature
from team.runners import FakeRuntime, _run, claude_cmd, grok_cmd
from team.util import extract_json, load_json
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

    def test_grok_write_capabilities_remain_path_scoped(self):
        extra = {"test_root": "tests", "code_root": "src/team"}
        tests_cmd = grok_cmd(
            prompt_path=Path("/tmp/p.md"),
            schema=None,
            capability="write-tests",
            session_id="s",
            resume=False,
            repo=Path("/tmp/repo"),
            extra=extra,
        )
        code_cmd = grok_cmd(
            prompt_path=Path("/tmp/p.md"),
            schema=None,
            capability="write-code",
            session_id="s",
            resume=False,
            repo=Path("/tmp/repo"),
            extra=extra,
        )
        tests_s = " ".join(str(x) for x in tests_cmd)
        code_s = " ".join(str(x) for x in code_cmd)
        self.assertIn("Edit(tests/**)", tests_s)
        self.assertIn("Edit(src/team/**)", code_s)
        self.assertNotEqual(tests_cmd, code_cmd)


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
