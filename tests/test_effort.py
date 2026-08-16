"""Per-role --effort is data, not a runtime name."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.config import EFFORT_MAX, EFFORT_MIN, load_config
from team.pipeline import start_feature
from team.runners import (
    CLAUDE_EFFORT_LADDER,
    GROK_EFFORT_LADDER,
    Result,
    _fake_output,
    claude_cmd,
    grok_cmd,
)
from tests.support.hostile import register_runtime
from tests.support.repo import init_repo


class RecordingRuntime:
    name = "fake"

    def __init__(self) -> None:
        self.extras = []

    def complete(self, **kwargs):
        extra = dict(kwargs.get("extra") or {})
        self.extras.append(extra)
        phase = kwargs.get("phase") or ""
        return Result(
            success=True,
            output=_fake_output(phase, extra),
            session_id="rec",
            raw="",
            num_turns=2,
        )


class EffortResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def _pipe(self, **kwargs):
        cfg = load_config(
            self.repo,
            fake=True,
            force=True,
            code_root="src",
            test_root="tests",
            **kwargs,
        )
        return start_feature(cfg, "brief", "effort-res")

    def test_default_high_only_for_reasoning_roles(self):
        rec = RecordingRuntime()
        pipe = self._pipe()
        with register_runtime("fake", rec):
            pipe.invoke("architect", "architect", "p", "design.json")
            pipe.invoke("scout", "scout", "p", "scout.json")
        self.assertEqual(rec.extras[0].get("effort"), 3)
        self.assertNotIn("effort", rec.extras[1])

    def test_config_override_and_caller_win(self):
        rec = RecordingRuntime()
        pipe = self._pipe(effort=["architect=xhigh", "scout=low"])
        with register_runtime("fake", rec):
            pipe.invoke("architect", "architect", "p", "design.json")
            pipe.invoke("scout", "scout", "p", "scout.json", extra={"effort": 2})
        self.assertEqual(rec.extras[0].get("effort"), 4)
        self.assertEqual(rec.extras[1].get("effort"), 2)

    def test_reviewer_both_identity_untouched(self):
        cfg = load_config(self.repo, effort=["reviewer=xhigh"])
        self.assertEqual(cfg.roles["reviewer"], "both")
        self.assertEqual(cfg.effort_for("reviewer"), 4)
        self.assertNotIn("grokxhigh", cfg.roles.values())


class EffortCmdTests(unittest.TestCase):
    def test_unset_effort_omits_flag_on_both_clis(self):
        claude = claude_cmd(
            prompt="hi",
            schema=None,
            capability="read-only",
            session_id="s",
            resume=False,
        )
        grok = grok_cmd(
            prompt_path=Path("/tmp/p.md"),
            schema=None,
            capability="read-only",
            session_id="s",
            resume=False,
            repo=Path("/tmp/repo"),
        )
        self.assertNotIn("--effort", claude)
        self.assertNotIn("--effort", grok)

    def _effort_of(self, cmd):
        return cmd[cmd.index("--effort") + 1] if "--effort" in cmd else None

    def _both(self, level):
        claude = claude_cmd(
            prompt="hi",
            schema=None,
            capability="read-only",
            session_id="s",
            resume=False,
            extra={"effort": level},
        )
        grok = grok_cmd(
            prompt_path=Path("/tmp/p.md"),
            schema=None,
            capability="read-only",
            session_id="s",
            resume=False,
            repo=Path("/tmp/repo"),
            extra={"effort": level},
        )
        return self._effort_of(claude), self._effort_of(grok)

    def test_every_level_maps_to_a_rung_each_cli_accepts(self):
        """The pair that must agree: neutral 0..5 <-> each CLI's own vocabulary.

        Ladder contents are pinned against what the binaries report for an
        invalid value, so this fails if a vendor adds or drops a rung.
        """
        for level in range(EFFORT_MIN, EFFORT_MAX + 1):
            claude, grok = self._both(level)
            self.assertIn(claude, CLAUDE_EFFORT_LADDER, "level %d" % level)
            self.assertIn(grok, GROK_EFFORT_LADDER, "level %d" % level)

    def test_top_level_snaps_to_nearest_rung_where_a_runtime_lacks_it(self):
        """Level 5 exists on Claude ("max") and not on Grok, which stops at
        xhigh. The missing rung must degrade, never drop the flag."""
        self.assertNotIn("max", GROK_EFFORT_LADDER)
        claude, grok = self._both(EFFORT_MAX)
        self.assertEqual(claude, "max")
        self.assertEqual(grok, "xhigh")

    def test_zero_is_a_level_not_an_absence(self):
        claude, grok = self._both(0)
        self.assertEqual(claude, "low")
        self.assertEqual(grok, "low")

    def test_monotonic_no_rung_skipped_downward(self):
        """A higher neutral level never yields a lower rung on either CLI."""
        for ladder, index in ((CLAUDE_EFFORT_LADDER, 0), (GROK_EFFORT_LADDER, 1)):
            seen = [ladder[self._both(lv)[index]] for lv in range(EFFORT_MIN, EFFORT_MAX + 1)]
            self.assertEqual(seen, sorted(seen), seen)

    def test_named_and_numeric_spellings_reach_the_same_argv(self):
        self.assertEqual(self._both("xhigh"), self._both(4))
        self.assertEqual(self._both("high"), self._both(3))


if __name__ == "__main__":
    unittest.main()
