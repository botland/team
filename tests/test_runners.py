import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.runners import (
    Result,
    claude_cmd,
    describe_runtime_failure,
    grok_cmd,
    headless_env,
    resolve_session,
)


class HeadlessCmdTests(unittest.TestCase):
    def test_claude_print_not_tui(self):
        cmd = claude_cmd(
            prompt="hi",
            schema=None,
            capability="read-only",
            session_id="s",
            resume=False,
        )
        self.assertEqual(cmd[1], "-p")
        self.assertIn("--output-format", cmd)
        self.assertIn("json", cmd)
        self.assertNotIn("--fullscreen", cmd)

    def test_grok_prompt_file_and_no_alt_screen(self):
        path = Path("/tmp/prompt.md")
        cmd = grok_cmd(
            prompt_path=path,
            schema=None,
            capability="read-only",
            session_id="s",
            resume=False,
            repo=Path("/tmp/repo"),
        )
        self.assertIn("--no-alt-screen", cmd)
        self.assertIn("--prompt-file", cmd)
        self.assertIn(str(path), cmd)
        self.assertIn("--output-format", cmd)
        self.assertNotIn("--fullscreen", cmd)
        self.assertNotIn("--minimal", cmd)

    def test_existing_session_resumes_instead_of_recreate(self):
        sid, resume = resolve_session("081ae15d-3f75-4a54-9c88-efb41c319de9")
        self.assertEqual(sid, "081ae15d-3f75-4a54-9c88-efb41c319de9")
        self.assertTrue(resume)
        sid, resume = resolve_session("")
        self.assertEqual(sid, "")
        self.assertFalse(resume)

    def test_claude_resume_flag_not_session_id_create(self):
        cmd = claude_cmd(
            prompt="hi",
            schema=None,
            capability="read-only",
            session_id="081ae15d-3f75-4a54-9c88-efb41c319de9",
            resume=True,
        )
        self.assertIn("--resume", cmd)
        self.assertNotIn("--session-id", cmd)

    def test_grok_resume_flag_not_session_id_create(self):
        cmd = grok_cmd(
            prompt_path=Path("/tmp/prompt.md"),
            schema=None,
            capability="read-only",
            session_id="081ae15d-3f75-4a54-9c88-efb41c319de9",
            resume=True,
            repo=Path("/tmp/repo"),
        )
        self.assertIn("-r", cmd)
        self.assertIn("081ae15d-3f75-4a54-9c88-efb41c319de9", cmd)
        # --session-id would collide; resume uses -r only
        if "--session-id" in cmd:
            self.fail("grok resume must not pass --session-id")

    def test_describe_runtime_failure_session_limit(self):
        payload = {
            "is_error": True,
            "api_error_status": 429,
            "result": "You've hit your session limit · resets 4:30pm (UTC)",
        }
        err = describe_runtime_failure(
            Result(
                success=False,
                output={},
                session_id="s",
                raw=__import__("json").dumps(payload),
                error="exit 1",
            )
        )
        self.assertIn("session limit", err.lower())
        self.assertNotIn("is_error", err)
        self.assertLess(len(err), 200)

    def test_headless_env_sets_ci(self):
        env = headless_env({"PATH": "/bin"})
        self.assertEqual(env["CI"], "1")
        self.assertEqual(env["TEAM_HEADLESS"], "1")
        self.assertEqual(env["PATH"], "/bin")


if __name__ == "__main__":
    unittest.main()
