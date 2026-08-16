"""Regressions for the /tmp/review/claude findings against fd3021f.

One file per finding class, each naming the fail direction, because every one of
these is silent in production: a dead fallback stamps a tag that records nothing,
an UNVERIFIED suite routed as a failure spends model hops repairing a bug that was
never observed, and a --dry-run that edits the queue changes what the next real
run does.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.cli import stamp_message
from team.config import load_config
from team.findings import collect_review_findings
from team.pipeline import start_feature
from team.testhost import compare, render_report
from team.util import dump_json
from tests.support.repo import init_repo


class StampMessageTests(unittest.TestCase):
    """Bug 5: `%` binds before `or`, so the reviewer fallback was unreachable."""

    def _field(self, message: str, key: str) -> str:
        for line in message.splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        raise AssertionError("no %s= line in %r" % (key, message))

    def _msg(self, **over):
        args = dict(
            slug="s",
            head_sha="abc123",
            runtimes=["claude", "grok"],
            assignment="both",
            findings_count=3,
            guardian_status="ok",
            range_base="v1",
        )
        args.update(over)
        return stamp_message(**args)

    def test_runtimes_are_listed_when_present(self):
        self.assertEqual(self._field(self._msg(), "reviewer"), "claude,grok")

    def test_empty_runtimes_falls_back_to_the_assignment(self):
        """The whole point of the fallback. With the precedence bug this line
        reads "reviewer=" and the tag records nothing about who reviewed."""
        msg = self._msg(runtimes=[])
        self.assertEqual(self._field(msg, "reviewer"), "both")
        self.assertNotIn("reviewer=\n", msg + "\n")

    def test_empty_range_base_falls_back_to_root(self):
        self.assertEqual(self._field(self._msg(range_base=""), "range-base"), "(root)")


class ReportFallbackTests(unittest.TestCase):
    """Bug 5, second site: the same precedence slip in the test report."""

    def test_empty_new_failures_renders_none_not_blank(self):
        base = {"status": "FAIL", "failing": ["t_a"], "names_unparsed": False}
        final = {"status": "FAIL", "failing": ["t_a"], "names_unparsed": False}
        text = render_report("Apply test run", final, compare(base, final))
        self.assertIn("- new failures: (none)", text)
        self.assertNotIn("- new failures: \n", text)

    def test_populated_new_failures_still_lists_names(self):
        base = {"status": "PASS", "failing": [], "names_unparsed": False}
        final = {"status": "FAIL", "failing": ["t_new"], "names_unparsed": False}
        text = render_report("Apply test run", final, compare(base, final))
        self.assertIn("t_new", text)
        self.assertNotIn("- new failures: (none)", text)


class CompareVerdictTests(unittest.TestCase):
    """A green final run is a PASS whatever the baseline said."""

    def test_pass_verdict_does_not_depend_on_the_baseline_status(self):
        final = {"status": "PASS", "failing": [], "names_unparsed": False}
        for base_status in ("PASS", "FAIL", "UNVERIFIED"):
            with self.subTest(baseline=base_status):
                base = {"status": base_status, "failing": ["t_a"]}
                self.assertEqual(compare(base, final)["verdict"], "PASS")


class DigestPinVacuityTests(unittest.TestCase):
    """An attempt that recorded nothing must not mean "trust every file"."""

    def _work(self, tmp: Path, last_review) -> Path:
        work = tmp / "work"
        (work / "prompts").mkdir(parents=True)
        dump_json(
            work / "prompts" / "reviewer-stale.result.json",
            {
                "summary": "left over from an earlier run",
                "findings": [
                    {
                        "severity": "high",
                        "title": "stale",
                        "evidence": "e",
                        "path": "src/a.py",
                        "kind": "implementation",
                    }
                ],
            },
        )
        state = {"slug": "s", "brief": "b", "repo": str(tmp), "engine_root": str(tmp)}
        if last_review is not None:
            state["last_review"] = last_review
        dump_json(work / "state.json", state)
        return work

    def test_recorded_attempt_with_no_results_collects_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self._work(Path(tmp), {"attempt": 2, "results": []})
            self.assertEqual(collect_review_findings(work), [])

    def test_no_attempt_recorded_still_reads_what_is_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = self._work(Path(tmp), None)
            self.assertEqual(
                [row["title"] for row in collect_review_findings(work)], ["stale"]
            )


class UnverifiedSuiteSkipTests(unittest.TestCase):
    """Bug 4: UNVERIFIED is not PASS, but it is not a failure either.

    Skipping the diagnose/repair rail only on PASS sends the debugger after a
    suite that never executed a case; the debugger names an owner, which
    un-skips repair, and an implementer rewrites production against no evidence.
    """

    def setUp(self):
        os.environ["TEAM_HOME"] = str(ROOT)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        cfg = load_config(self.repo, fake=True, force=True, code_root=".", test_root="tests")
        self.pipe = start_feature(cfg, "brief", "unverified")

    def test_unverified_skips_debugger_and_repair_with_a_reason(self):
        self.pipe.state.final = {"status": "UNVERIFIED", "output": "(no test command discovered)"}
        for phase in ("debugger", "repair"):
            reason = self.pipe._skip_reason(phase)
            self.assertTrue(reason, "%s must be skipped on UNVERIFIED" % phase)
            self.assertIn("UNVERIFIED", reason)

    def test_pass_still_skips_them_for_the_original_reason(self):
        self.pipe.state.final = {"status": "PASS"}
        self.assertEqual(self.pipe._skip_reason("debugger"), "tests passed")
        self.assertEqual(self.pipe._skip_reason("repair"), "tests passed")

    def test_a_proved_fail_still_reaches_the_debugger(self):
        """The converse: the fix must not skip the rail it exists to run."""
        self.pipe.state.final = {"status": "FAIL", "failing": ["t_a"]}
        self.assertEqual(self.pipe._skip_reason("debugger"), "")

    def test_missing_final_is_treated_as_no_evidence(self):
        self.pipe.state.final = {}
        self.assertIn("UNVERIFIED", self.pipe._skip_reason("debugger"))


class SchemaEnumSeamTests(unittest.TestCase):
    """Sug 2: closed sets that code branches on must be enums in the schema.

    Both CLIs receive these via --json-schema, so an enum is a constraint at
    generation time -- the highest rung available here. The seam is that the
    same set is also a Python dict the orchestrator routes on; neither side
    owns the agreement, so it is asserted rather than maintained by hand.

    Note the orchestrator validates with enums=False on purpose: the enum
    shapes what the vendor emits, it does not turn a stray value into a crash.
    """

    def _schema(self, name):
        import json

        from team.config import schema_path

        return json.loads(Path(schema_path(name)).read_text(encoding="utf-8"))

    def test_guardian_link_enum_matches_the_routing_table(self):
        from team.findings import _GUARDIAN_LINK_KIND

        schema = self._schema("guardian.json")
        enum = schema["properties"]["risks"]["items"]["properties"]["link"]["enum"]
        self.assertEqual(
            sorted(enum),
            sorted(_GUARDIAN_LINK_KIND),
            "schema enum and findings._GUARDIAN_LINK_KIND have drifted",
        )

    def test_guardian_chain_keys_are_the_arrow_links(self):
        """The converse: every arrow in the chain is a link a risk may carry."""
        schema = self._schema("guardian.json")
        chain = set(schema["properties"]["chain"]["properties"])
        enum = set(schema["properties"]["risks"]["items"]["properties"]["link"]["enum"])
        self.assertTrue(
            chain <= enum, "chain arrows missing from the link enum: %s" % (chain - enum)
        )

    def test_scout_state_enum_matches_the_persona(self):
        import re

        schema = self._schema("scout.json")
        enum = schema["properties"]["components"]["items"]["properties"]["state"]["enum"]
        persona = (ROOT / "personas" / "scout.md").read_text(encoding="utf-8")
        named = set(re.findall(r"`(done|wip|missing|external|broken)`", persona))
        self.assertTrue(named, "persona no longer names the state vocabulary")
        self.assertEqual(sorted(enum), sorted(named))

    def test_debugger_owner_enum_covers_the_auto_repair_owners(self):
        """The set the orchestrator branches on must be representable."""
        schema = self._schema("debugger.json")
        enum = set(schema["properties"]["owner"]["enum"])
        self.assertTrue({"implementer", "test-writer"} <= enum)
        self.assertIn("unknown", enum)

    def test_every_closed_set_field_is_an_enum_not_a_prose_description(self):
        """Census: a description listing values with | is a closed set that
        never reached the schema. review.json was the only enum in the tree."""
        import json

        from team.config import schema_path

        offenders = []
        for path in sorted((ROOT / "schemas").glob("*.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))

            def walk(node):
                if not isinstance(node, dict):
                    return
                desc = node.get("description") or ""
                if (
                    node.get("type") == "string"
                    and "enum" not in node
                    and re.search(r"\w \| \w", desc)
                ):
                    offenders.append("%s: %s" % (path.name, desc[:60]))
                for value in node.values():
                    if isinstance(value, dict):
                        walk(value)
                    elif isinstance(value, list):
                        for item in value:
                            walk(item)

            walk(schema)
        self.assertEqual(offenders, [], "closed set stated as prose, not enum")


if __name__ == "__main__":
    unittest.main()
