import json
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


_REVIEW_SCHEMA = {
    "type": "object",
    "required": ["findings", "summary"],
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array"},
    },
}

_FINDING = {
    "severity": "low",
    "title": "F82 residual",
    "evidence": "READMEs outside this range still say P3 is unbuilt",
    "kind": "note",
    "path": "FOLLOWUPS.md",
}

_REVIEW = {
    "summary": "P3 is in the tree. Highest leftover is a census that misses DELETE.",
    "findings": [
        {
            "severity": "high",
            "title": "Secret-store census does not see DELETE",
            "evidence": "delete_secret is not an anchor",
            "kind": "test",
            "path": "inferedge-phase1/tests/unit/test_a_secret_value_has_one_reader.py",
        },
        _FINDING,
    ],
    "review_markdown": "# Review\nP3 is in the tree.\n",
}


class ExtractJsonTests(unittest.TestCase):
    def test_plain_schema(self):
        self.assertEqual(extract_json('{"ready": true}'), {"ready": True})

    def test_claude_wrapper(self):
        raw = '{"type":"result","result":{"summary":"ok","paths_touched":[]}}'
        self.assertEqual(extract_json(raw)["summary"], "ok")

    def test_grok_text_field(self):
        raw = '{"text":"{\\"accepts\\": true}","sessionId":"abc"}'
        self.assertTrue(extract_json(raw)["accepts"])

    def test_trailing_eos_does_not_collapse_to_a_nested_finding(self):
        raw = json.dumps(_REVIEW) + "<|eos|>"
        out = extract_json(raw)
        self.assertEqual(out["summary"], _REVIEW["summary"])
        self.assertEqual(len(out["findings"]), 2)

    def test_concatenated_reviews_keep_last_top_level_object(self):
        placeholder = {"summary": "reading", "findings": []}
        raw = json.dumps(placeholder) + json.dumps(_REVIEW)
        out = extract_json(raw)
        self.assertEqual(out["summary"], _REVIEW["summary"])
        self.assertEqual(len(out["findings"]), 2)

    def test_grok_text_concatenates_placeholder_then_final_review(self):
        placeholder = {"summary": "Reading the secret-route census.", "findings": []}
        envelope = {
            "text": json.dumps(placeholder) + json.dumps(_REVIEW) + "<|eos|>",
            "sessionId": "ebba3c9a-1b38-4490-883e-a3e9e7400901",
            "stopReason": "end_turn",
        }
        out = extract_json(json.dumps(envelope), schema=_REVIEW_SCHEMA)
        self.assertEqual(out["summary"], _REVIEW["summary"])
        self.assertEqual(len(out["findings"]), 2)
        self.assertEqual(out["findings"][-1]["title"], _FINDING["title"])

    def test_structured_output_json_string(self):
        envelope = {
            "structuredOutput": json.dumps(_REVIEW),
            "text": json.dumps({"summary": "investigating", "findings": []}),
            "sessionId": "abc",
        }
        out = extract_json(json.dumps(envelope), schema=_REVIEW_SCHEMA)
        self.assertEqual(out["summary"], _REVIEW["summary"])

    def test_schema_skips_finding_shaped_structured_output(self):
        envelope = {
            "structuredOutput": _FINDING,
            "text": json.dumps(_REVIEW) + "<|eos|>",
            "sessionId": "abc",
        }
        out = extract_json(json.dumps(envelope), schema=_REVIEW_SCHEMA)
        self.assertEqual(out["summary"], _REVIEW["summary"])
        self.assertEqual(len(out["findings"]), 2)


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
