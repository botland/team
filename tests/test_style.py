import os
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team import style


class StyleTests(unittest.TestCase):
    def test_no_color_wins(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1", "FORCE_COLOR": "1"}):
            self.assertFalse(style.color_enabled())
            self.assertEqual(style.severity("high"), "high")

    def test_force_color_without_tty(self):
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1", "NO_COLOR": ""}):
            self.assertTrue(style.color_enabled(StringIO()))
            painted = style.severity("invariant")
            self.assertIn("\033", painted)
            self.assertTrue(painted.endswith(style.RESET))
            self.assertEqual(style.strip_ansi(painted), "invariant")

    def test_stringio_is_not_a_tty(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "", "FORCE_COLOR": ""}, clear=False):
            os.environ.pop("NO_COLOR", None)
            os.environ.pop("FORCE_COLOR", None)
            self.assertFalse(style.color_enabled(StringIO()))

    def test_palette_and_ljust_visible_width(self):
        self.assertIn(style.RED, style.severity("high", enabled=True))
        self.assertIn(style.BRIGHT_BLUE, style.kind("architecture", enabled=True))
        self.assertIn(style.GREEN, style.status("applied", enabled=True))
        self.assertIn(style.BRIGHT_GREEN, style.link("t_to_i", enabled=True))
        tagged = style.link_tags("see [t_to_i] then [i_to_r]", enabled=True)
        self.assertIn(style.BRIGHT_GREEN, tagged)
        self.assertIn(style.BRIGHT_MAGENTA, tagged)
        self.assertEqual(
            style.strip_ansi(tagged),
            "see [t_to_i] then [i_to_r]",
        )
        cell = style.ljust("failed", 10, style.status, enabled=True)
        self.assertTrue(cell.endswith("    "))
        self.assertEqual(len(style.strip_ansi(cell)), 10)
        self.assertEqual(style.tag_pair("high", "test", enabled=False), "high/test")
        self.assertEqual(style.tag_pair("?", "", enabled=False), "?")


if __name__ == "__main__":
    unittest.main()
