import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.runners import claude_cmd, grok_cmd, headless_env


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

    def test_headless_env_sets_ci(self):
        env = headless_env({"PATH": "/bin"})
        self.assertEqual(env["CI"], "1")
        self.assertEqual(env["TEAM_HEADLESS"], "1")
        self.assertEqual(env["PATH"], "/bin")


if __name__ == "__main__":
    unittest.main()
