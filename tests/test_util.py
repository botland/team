import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.merge import merge_reviews
from team.util import extract_json, slugify, under_root


class SlugTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Add OAuth login with Google"), "add-oauth-login-with-google")

    def test_empty(self):
        self.assertEqual(slugify("???"), "feature")

    def test_truncates(self):
        self.assertLessEqual(len(slugify("a " * 80)), 40)


class PathTests(unittest.TestCase):
    def test_under_root(self):
        self.assertTrue(under_root("tests/test_a.py", "tests"))
        self.assertFalse(under_root("src/a.py", "tests"))
        self.assertTrue(under_root("src/a.py", ""))
        self.assertTrue(under_root(".team/work/foo/design.md", ".team/work"))
        self.assertTrue(under_root("./tests/a.py", "tests"))


class ExtractJsonTests(unittest.TestCase):
    def test_plain_schema(self):
        self.assertEqual(extract_json('{"ready": true}'), {"ready": True})

    def test_claude_wrapper(self):
        raw = '{"type":"result","result":{"summary":"ok","paths_touched":[]}}'
        self.assertEqual(extract_json(raw)["summary"], "ok")

    def test_grok_text_field(self):
        raw = '{"text":"{\\"accepts\\": true}","sessionId":"abc"}'
        self.assertTrue(extract_json(raw)["accepts"])


class MergeTests(unittest.TestCase):
    def test_overlap(self):
        a = {
            "summary": "A",
            "findings": [
                {
                    "severity": "high",
                    "title": "leak",
                    "evidence": "x",
                    "path": "src/a.py",
                    "kind": "implementation",
                }
            ],
        }
        b = {
            "summary": "B",
            "findings": [
                {
                    "severity": "high",
                    "title": "leak",
                    "evidence": "y",
                    "path": "src/a.py",
                    "kind": "implementation",
                }
            ],
        }
        md = merge_reviews([("claude", a, "body a"), ("grok", b, "body b")])
        self.assertIn("src/a.py", md)
        self.assertIn("[implementation]", md)
        self.assertIn("## claude", md)
        self.assertIn("## grok", md)


if __name__ == "__main__":
    unittest.main()
