"""Provider spend: envelope is the only source. Missing $ is not free."""

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

from team import style
from team.cli import main
from team.config import load_config, schema_path
from team.pipeline import PipelineError, start_feature
from team.runners import FakeRuntime, Result, _run
from team.usage import (
    USAGE_JSONL,
    USAGE_MD,
    Usage,
    format_hop_console,
    format_summary_line,
    format_usd,
    hop_has_cost,
    hop_record,
    load_hops,
    load_repo_hops,
    parse_usage,
    summarize,
)
from team.util import load_json
from tests.support.hostile import HostileRuntime, emit, register_runtime
from tests.support.repo import init_repo


GROK_ENVELOPE = {
    "text": json.dumps({"summary": "ok", "findings": []}),
    "sessionId": "sid-from-grok",
    "stopReason": "end_turn",
    "num_turns": 7,
    "usage": {
        "input_tokens": 7210,
        "cache_read_input_tokens": 41000,
        "cache_creation_input_tokens": 0,
        "output_tokens": 1893,
        "reasoning_tokens": 412,
        "total_tokens": 50103,
    },
    "modelUsage": {
        "grok-build": {
            "inputTokens": 7210,
            "outputTokens": 1893,
            "cacheReadInputTokens": 41000,
            "modelCalls": 7,
            "costUSD": 0.01268905,
        }
    },
    "total_cost_usd": 0.01268905,
    "total_cost_usd_ticks": 126890500,
}

CLAUDE_ENVELOPE = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 5,
    "session_id": "sid-from-claude",
    "result": json.dumps({"summary": "ok", "findings": []}),
    "total_cost_usd": 0.05,
    "usage": {
        "input_tokens": 1000,
        "output_tokens": 300,
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 5000,
    },
}


class ParseUsageTests(unittest.TestCase):
    def test_grok_envelope_tokens_and_cost(self):
        usage = parse_usage(GROK_ENVELOPE)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.input_tokens, 7210)
        self.assertEqual(usage.output_tokens, 1893)
        self.assertEqual(usage.cache_read_input_tokens, 41000)
        self.assertEqual(usage.total_tokens, 50103)
        self.assertEqual(usage.cost_usd, 0.01268905)
        self.assertEqual(usage.cost_usd_ticks, 126890500)
        self.assertTrue(usage.has_cost())
        self.assertTrue(usage.has_tokens())

    def test_claude_envelope_tokens_and_cost(self):
        usage = parse_usage(CLAUDE_ENVELOPE)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.input_tokens, 1000)
        self.assertEqual(usage.output_tokens, 300)
        self.assertEqual(usage.cost_usd, 0.05)
        self.assertTrue(usage.has_cost())
        self.assertEqual(usage.total_tokens, 1000 + 300 + 200 + 5000)

    def test_missing_cost_is_unknown_not_free(self):
        envelope = {
            "usage": {"input_tokens": 10, "output_tokens": 4},
            "num_turns": 2,
        }
        usage = parse_usage(envelope)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.input_tokens, 10)
        self.assertIsNone(usage.cost_usd)
        self.assertFalse(usage.has_cost())
        self.assertNotEqual(usage.cost_usd, 0)
        self.assertEqual(format_usd(usage.cost_usd), "$ unknown")

    def test_cost_is_partial_drops_every_cost_float(self):
        envelope = {
            "usage": {"input_tokens": 10, "output_tokens": 1},
            "total_cost_usd": 0.4,
            "cost_is_partial": True,
            "modelUsage": {"grok-build": {"costUSD": 0.4}},
        }
        usage = parse_usage(envelope)
        self.assertIsNotNone(usage)
        self.assertTrue(usage.cost_is_partial)
        self.assertIsNone(usage.cost_usd)
        self.assertFalse(usage.has_cost())

    def test_usage_is_incomplete_drops_cost(self):
        envelope = {
            "usage": {"input_tokens": 10, "output_tokens": 1},
            "total_cost_usd": 0.4,
            "usage_is_incomplete": True,
        }
        usage = parse_usage(envelope)
        self.assertIsNotNone(usage)
        self.assertTrue(usage.usage_is_incomplete)
        self.assertIsNone(usage.cost_usd)
        self.assertFalse(usage.has_cost())

    def test_model_usage_cost_is_not_the_hop_bill(self):
        envelope = {
            "usage": {"input_tokens": 10, "output_tokens": 1},
            "modelUsage": {
                "grok-build": {"costUSD": 0.4},
                "grok-other": {"costUSD": 0.6},
            },
        }
        usage = parse_usage(envelope)
        self.assertIsNotNone(usage)
        self.assertIsNone(usage.cost_usd)
        self.assertFalse(usage.has_cost())

    def test_no_spend_fields_is_none(self):
        self.assertIsNone(parse_usage({"text": "hi", "sessionId": "x"}))
        self.assertIsNone(parse_usage(None))
        self.assertIsNone(parse_usage("nope"))

    def test_incomplete_without_tokens_is_still_a_usage(self):
        usage = parse_usage({"usage_is_incomplete": True})
        self.assertIsNotNone(usage)
        self.assertTrue(usage.usage_is_incomplete)
        self.assertFalse(usage.has_tokens())
        self.assertFalse(usage.has_cost())


class SummarizeTests(unittest.TestCase):
    def test_complete_bill_sums_ticks(self):
        hops = [
            {
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
                "cost_usd": 0.01,
                "cost_usd_ticks": 100_000_000,
            },
            {
                "input_tokens": 20,
                "output_tokens": 3,
                "total_tokens": 23,
                "cost_usd": 0.02,
                "cost_usd_ticks": 200_000_000,
            },
        ]
        summary = summarize(hops)
        self.assertTrue(summary["cost_complete"])
        self.assertEqual(summary["hops_with_cost"], 2)
        self.assertEqual(summary["input_tokens"], 30)
        self.assertAlmostEqual(summary["cost_usd"], 0.03)

    def test_partial_bill_is_not_complete(self):
        hops = [
            {"input_tokens": 10, "output_tokens": 1, "cost_usd": 0.5},
            {"input_tokens": 10, "output_tokens": 1},
        ]
        summary = summarize(hops)
        self.assertFalse(summary["cost_complete"])
        self.assertEqual(summary["hops_missing_cost"], 1)
        self.assertAlmostEqual(summary["cost_usd"], 0.5)
        self.assertIn("omitted", format_usd(summary["cost_usd"], complete=False, missing=1))

    def test_zero_reported_cost_is_kept(self):
        hops = [{"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}]
        summary = summarize(hops)
        self.assertTrue(summary["cost_complete"])
        self.assertEqual(summary["cost_usd"], 0.0)
        self.assertTrue(hop_has_cost(hops[0]))


class ConsoleColorTests(unittest.TestCase):
    def test_hop_line_paints_dollars_and_tokens(self):
        rec = hop_record(
            slug="s",
            phase="architect",
            role="architect",
            runtime="grok",
            session_id="x",
            success=True,
            num_turns=2,
            usage=Usage(input_tokens=7200, output_tokens=1900, cost_usd=0.25),
        )
        plain = format_hop_console(rec, enabled=False)
        painted = format_hop_console(rec, enabled=True)
        self.assertEqual(style.strip_ansi(painted), plain)
        self.assertIn(style.BRIGHT_GREEN, painted)
        self.assertIn(style.CYAN, painted)
        self.assertNotIn("\033", plain)

    def test_unknown_cost_is_yellow_not_green(self):
        rec = hop_record(
            slug="s",
            phase="reviewer",
            role="reviewer",
            runtime="grok",
            session_id="x",
            success=True,
            num_turns=3,
            usage=Usage(input_tokens=10, output_tokens=1),
        )
        painted = format_hop_console(rec, enabled=True)
        self.assertIn(style.YELLOW, painted)
        self.assertNotIn(style.BRIGHT_GREEN, painted)
        self.assertIn("$ unknown", style.strip_ansi(painted))


class RunEnvelopeUsageTests(unittest.TestCase):
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

    def _run_envelope(self, envelope, *, exit_code=0):
        payload = self.root / "stdout.json"
        payload.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
        script = self._script("cat '%s'\nexit %d" % (payload, exit_code))
        return _run(
            [str(script)],
            repo=self.root,
            timeout=5,
            session_id="s",
            prompt_path=self.prompt,
            schema=load_json(schema_path("review.json")),
        )

    def test_grok_envelope_usage_is_on_result(self):
        result = self._run_envelope(GROK_ENVELOPE)
        self.assertTrue(result.success)
        self.assertIsNotNone(result.usage)
        self.assertEqual(result.usage.input_tokens, 7210)
        self.assertEqual(result.usage.cost_usd, 0.01268905)
        self.assertEqual(result.num_turns, 7)

    def test_failed_hop_still_keeps_spend(self):
        result = self._run_envelope(GROK_ENVELOPE, exit_code=2)
        self.assertFalse(result.success)
        self.assertIsNotNone(result.usage)
        self.assertEqual(result.usage.input_tokens, 7210)
        self.assertEqual(result.usage.cost_usd, 0.01268905)

    def test_envelope_without_spend_leaves_usage_none(self):
        result = self._run_envelope(
            {
                "text": json.dumps({"summary": "ok", "findings": []}),
                "sessionId": "x",
                "num_turns": 2,
            }
        )
        self.assertTrue(result.success)
        self.assertIsNone(result.usage)


class InvokeLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def _pipe(self, slug="usage-inv"):
        cfg = load_config(
            self.repo, fake=True, force=True, code_root="src", test_root="tests"
        )
        return start_feature(cfg, "brief", slug)

    def test_invoke_writes_ledger_meta_and_log(self):
        usage = Usage(input_tokens=100, output_tokens=20, total_tokens=120, cost_usd=0.25)
        hostile = HostileRuntime(
            [emit({"design_markdown": "# D", "code_root": "src", "test_root": "tests", "census_markdown": "# C"})],
            phases=("architect",),
            num_turns=2,
            usage=usage,
        )
        pipe = self._pipe()
        with register_runtime("fake", hostile):
            pipe.invoke("architect", "architect", "p", "design.json")
        hops = load_hops(pipe.work)
        self.assertEqual(len(hops), 1)
        self.assertEqual(hops[0]["phase"], "architect")
        self.assertEqual(hops[0]["input_tokens"], 100)
        self.assertEqual(hops[0]["cost_usd"], 0.25)
        self.assertTrue((pipe.work / USAGE_JSONL).is_file())
        md = (pipe.work / USAGE_MD).read_text(encoding="utf-8")
        self.assertIn("architect", md)
        self.assertIn("0.2500", md)
        meta = load_json(pipe.work / "prompts" / "architect.result.json")["_meta"]
        self.assertEqual(meta["usage"]["input_tokens"], 100)
        self.assertEqual(meta["usage"]["cost_usd"], 0.25)
        log = style.strip_ansi("\n".join(pipe.log_lines))
        self.assertIn("usage  architect", log)
        self.assertIn("$0.25", log)
        self.assertIn("1 hop", log)

    def test_failed_invoke_still_records_spend(self):
        usage = Usage(input_tokens=9, output_tokens=1, cost_usd=0.11)
        hostile = HostileRuntime(
            [emit({"summary": "ok"})],
            phases=("reviewer-fake",),
            num_turns=2,
            usage=usage,
        )
        pipe = self._pipe("usage-fail")
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError):
                pipe.invoke("reviewer", "reviewer-fake", "p", "review.json")
        hops = load_hops(pipe.work)
        self.assertEqual(len(hops), 1, "schema failure must not drop the spend row")
        self.assertEqual(hops[0]["cost_usd"], 0.11)
        self.assertEqual(hops[0]["input_tokens"], 9)

    def test_fake_runtime_does_not_invent_a_bill(self):
        pipe = self._pipe("usage-fake")
        result = FakeRuntime().complete(
            role="architect",
            phase="architect",
            prompt="p",
            schema=None,
            capability="read-only",
            work=pipe.work,
            repo=self.repo,
        )
        self.assertIsNone(result.usage)
        pipe.invoke("architect", "architect", "p", "design.json")
        hops = load_hops(pipe.work)
        self.assertEqual(len(hops), 1, "missing spend still gets a hop row")
        self.assertNotIn("cost_usd", hops[0])
        self.assertIn("spend omitted", format_hop_console(hops[0], enabled=False))
        self.assertIsNone(
            load_json(pipe.work / "prompts" / "architect.result.json")
            .get("_meta", {})
            .get("usage")
        )

    def test_two_hops_same_phase_are_two_ledger_rows(self):
        usage = Usage(input_tokens=1, output_tokens=1, cost_usd=0.01)
        hostile = HostileRuntime(
            [emit({"design_markdown": "# D", "code_root": "src", "test_root": "tests"})],
            phases=("architect",),
            num_turns=2,
            usage=usage,
        )
        pipe = self._pipe("usage-retry")
        with register_runtime("fake", hostile):
            pipe.invoke("architect", "architect", "p", "design.json")
            pipe.invoke("architect", "architect", "p", "design.json")
        hops = load_hops(pipe.work)
        self.assertEqual(len(hops), 2)
        self.assertEqual([h["phase"] for h in hops], ["architect", "architect"])
        summary = summarize(hops)
        self.assertAlmostEqual(summary["cost_usd"], 0.02)
        self.assertTrue(summary["cost_complete"])


class CostsCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def test_costs_prints_slug_ledger(self):
        cfg = load_config(
            self.repo, fake=True, force=True, code_root="src", test_root="tests"
        )
        pipe = start_feature(cfg, "brief", "usage-cmd")
        usage = Usage(input_tokens=2000, output_tokens=100, cost_usd=1.5)
        hostile = HostileRuntime(
            [emit({"design_markdown": "# D", "code_root": "src", "test_root": "tests", "census_markdown": "# C"})],
            phases=("architect",),
            num_turns=2,
            usage=usage,
        )
        with register_runtime("fake", hostile):
            pipe.invoke("architect", "architect", "p", "design.json")
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = main(["--repo", str(self.repo), "costs", "usage-cmd"])
        self.assertEqual(rc, 0)
        text = style.strip_ansi(buf.getvalue())
        self.assertIn("architect", text)
        self.assertIn("$1.50", text)
        self.assertIn("usage.jsonl", text)

    def test_force_does_not_erase_repo_ledger(self):
        import shutil

        cfg = load_config(
            self.repo, fake=True, force=True, code_root="src", test_root="tests"
        )
        pipe = start_feature(cfg, "brief", "usage-force")
        usage = Usage(input_tokens=100, output_tokens=10, cost_usd=0.25)
        hostile = HostileRuntime(
            [emit({"design_markdown": "# D", "code_root": "src", "test_root": "tests"})],
            phases=("architect",),
            num_turns=2,
            usage=usage,
        )
        with register_runtime("fake", hostile):
            pipe.invoke("architect", "architect", "p", "design.json")
        ledger = self.repo / ".team" / "work" / USAGE_JSONL
        self.assertTrue(ledger.is_file(), "durable ledger next to slug dirs")
        shutil.rmtree(pipe.work)
        self.assertFalse((pipe.work / USAGE_JSONL).is_file())
        hops = load_repo_hops(self.repo, slug="usage-force")
        self.assertEqual(len(hops), 1)
        self.assertEqual(hops[0]["cost_usd"], 0.25)
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = main(["--repo", str(self.repo), "costs", "usage-force"])
        self.assertEqual(rc, 0)
        self.assertIn("$0.25", style.strip_ansi(buf.getvalue()))

    def test_apply_error_still_prints_spend(self):
        cfg = load_config(
            self.repo, fake=True, force=True, code_root="src", test_root="tests"
        )
        pipe = start_feature(cfg, "brief", "usage-err")
        usage = Usage(input_tokens=100, output_tokens=10, cost_usd=0.25)
        hostile = HostileRuntime(
            [emit({"design_markdown": "# D", "code_root": "src", "test_root": "tests"})],
            phases=("architect",),
            num_turns=2,
            usage=usage,
        )
        with register_runtime("fake", hostile):
            pipe.invoke("architect", "architect", "p", "design.json")
        buf = StringIO()
        err = StringIO()
        with mock.patch(
            "team.cli.load_pipeline", side_effect=PipelineError("tdd-design-apply wrote outside")
        ):
            with mock.patch("sys.stdout", buf), mock.patch("sys.stderr", err):
                rc = main(["--repo", str(self.repo), "apply", "usage-err"])
        self.assertEqual(rc, 1)
        self.assertIn("tdd-design-apply", err.getvalue())
        self.assertIn("$0.25", style.strip_ansi(buf.getvalue()))

    def test_costs_lists_runs(self):
        cfg = load_config(
            self.repo, fake=True, force=True, code_root="src", test_root="tests"
        )
        pipe = start_feature(cfg, "brief", "usage-list")
        usage = Usage(input_tokens=100, output_tokens=10, cost_usd=0.2)
        hostile = HostileRuntime(
            [emit({"design_markdown": "# D", "code_root": "src", "test_root": "tests", "census_markdown": "# C"})],
            phases=("architect",),
            num_turns=2,
            usage=usage,
        )
        with register_runtime("fake", hostile):
            pipe.invoke("architect", "architect", "p", "design.json")
        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            rc = main(["--repo", str(self.repo), "costs"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("usage-list", text)
        self.assertIn("$0.20", text)

    def test_costs_missing_slug_is_an_error(self):
        buf = StringIO()
        err = StringIO()
        with mock.patch("sys.stdout", buf), mock.patch("sys.stderr", err):
            rc = main(["--repo", str(self.repo), "costs", "no-such"])
        self.assertEqual(rc, 1)
        self.assertIn("No run", err.getvalue())


if __name__ == "__main__":
    unittest.main()
