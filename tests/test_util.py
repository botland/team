import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.merge import merge_reviews
from team.testhost import _python_fallback_root
from team.util import (
    denied_write_code_roots,
    explicit_roots,
    extract_json,
    normalize_root,
    slugify,
    under_root,
)

_SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


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

    def test_normalize_root_is_one_encoding(self):
        self.assertEqual(normalize_root("tests/"), "tests")
        self.assertEqual(normalize_root("./tests/"), "tests")
        self.assertEqual(normalize_root("tests"), "tests")
        self.assertEqual(normalize_root("."), ".")
        self.assertEqual(normalize_root("./"), ".")
        self.assertEqual(normalize_root("  "), "")
        self.assertEqual(explicit_roots("tests/"), ["tests"])
        self.assertEqual(explicit_roots(["./src/", "tests/"]), ["src", "tests"])
        self.assertEqual(
            denied_write_code_roots(".", "tests/", ["appliance-console/"]),
            ["tests", "appliance-console"],
        )

    def test_denied_write_code_dot_excludes_tests_and_foreign_submodules(self):
        self.assertEqual(
            denied_write_code_roots(".", "tests", ["appliance-console", "appliance-support"]),
            ["tests", "appliance-console", "appliance-support"],
        )

    def test_denied_write_code_inside_submodule_keeps_that_submodule(self):
        self.assertEqual(
            denied_write_code_roots(
                "appliance-console",
                "appliance-console/tests",
                ["appliance-console", "appliance-support"],
            ),
            ["appliance-console/tests", "appliance-support"],
        )
        self.assertEqual(
            denied_write_code_roots(
                "appliance-console/app",
                "tests",
                ["appliance-console"],
            ),
            ["tests"],
        )

    def test_denied_write_code_skips_a_test_root_that_is_the_code_root(self):
        """A root cannot deny itself, and '.' as test_root denies nothing.

        The unset case is deliberately *not* here: it resolves through
        testhost's fallback and is pinned by
        test_denied_write_code_dot_unset_test_root_includes_testhost_fallback.
        The two must not disagree -- an empty test_root naming no denial is how
        an implementer reaches the conventional tests tree.
        """
        self.assertEqual(denied_write_code_roots(".", ".", ["vendor"]), ["vendor"])
        self.assertEqual(denied_write_code_roots("src", "src", []), [])
        self.assertEqual(denied_write_code_roots("tests", "tests", []), [])
        # Unset resolves to the fallback for every code_root, not only '.':
        # the deny list is one rule, not a rule plus an exception.
        self.assertEqual(
            denied_write_code_roots("src", "", []),
            [_python_fallback_root("")],
        )

    def test_denied_write_code_dot_unset_test_root_includes_testhost_fallback(self):
        """code_root='.' + unset test_root denies testhost's conventional tree.

        Explicit test_root='tests' does not close the unset case. Membership
        is testhost's fallback, not a second hand list.
        """
        fallback = _python_fallback_root("")
        self.assertTrue(fallback)
        self.assertEqual(_python_fallback_root("  "), fallback)
        for raw in ("", "  ", "\t", None):
            with self.subTest(test_root=raw):
                denied = denied_write_code_roots(".", raw, [])
                self.assertIn(fallback, denied)
                self.assertTrue(
                    any(under_root("%s/a.py" % fallback, root) for root in denied),
                    denied,
                )


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


def _top_level_arrays(schema):
    props = schema.get("properties") or {}
    return [
        key
        for key, spec in props.items()
        if isinstance(spec, dict) and spec.get("type") == "array"
    ]


def _load_schema(name):
    return json.loads((_SCHEMAS / name).read_text(encoding="utf-8"))


def _array_schemas():
    named = []
    for path in sorted(_SCHEMAS.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        if _top_level_arrays(schema):
            named.append((path.name, schema))
    return named


def _finished_empty(schema):
    return _schema_payload(schema, complete=False)


def _complete_payload(schema):
    return _schema_payload(schema, complete=True)


def _incomplete_payload(schema):
    finished = _finished_empty(schema)
    arrays = set(_top_level_arrays(schema))
    drop = [key for key in (schema.get("required") or []) if key not in arrays]
    if drop:
        obj = dict(finished)
        obj.pop(drop[0], None)
        return obj
    return {}


def _fill_spec(spec, *, complete):
    if not isinstance(spec, dict):
        return "x"
    typ = spec.get("type")
    if typ == "array":
        return [_fill_spec(spec.get("items") or {}, complete=True)] if complete else []
    if typ == "boolean":
        return True
    if typ == "object" or "properties" in spec or "required" in spec:
        return _schema_payload(spec, complete=complete)
    if isinstance(typ, list):
        if "boolean" in typ:
            return True
        if "null" in typ and "boolean" in typ:
            return True
    return "x"


def _schema_payload(schema, *, complete):
    props = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    obj = {}
    for key in required:
        spec = props.get(key) or {}
        if isinstance(spec, dict) and spec.get("type") == "array":
            obj[key] = _fill_spec(spec, complete=complete)
        else:
            obj[key] = _fill_spec(spec, complete=False)
    for key in _top_level_arrays(schema):
        obj[key] = _fill_spec(props[key], complete=complete)
    return obj


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
        """Converse envelope: complete wrapper, vacuous text. Half the contract."""
        envelope = {
            "structuredOutput": json.dumps(_REVIEW),
            "text": json.dumps({"summary": "investigating", "findings": []}),
            "sessionId": "abc",
        }
        out = extract_json(json.dumps(envelope), schema=_REVIEW_SCHEMA)
        self.assertEqual(out["summary"], _REVIEW["summary"])
        self.assertEqual(len(out["findings"]), len(_REVIEW["findings"]))

    def _assert_review(self, out):
        self.assertEqual(out["summary"], _REVIEW["summary"])
        self.assertEqual(len(out["findings"]), len(_REVIEW["findings"]))
        self.assertEqual(out["findings"][-1]["title"], _FINDING["title"])

    def test_finished_empty_review_structured_output_beats_stale_text_findings(self):
        schema = _load_schema("review.json")
        finished = {
            "findings": [],
            "summary": "No issues.",
            "review_markdown": "No issues.",
        }
        stale = _REVIEW
        wrappers = ("structuredOutput", "structured_output", "result")
        for key in wrappers:
            for structured in (finished, json.dumps(finished)):
                with self.subTest(wrapper=key, structured=type(structured).__name__):
                    envelope = {
                        key: structured,
                        "text": json.dumps(stale),
                        "sessionId": "abc",
                    }
                    out = extract_json(json.dumps(envelope), schema=schema)
                    self.assertEqual(out["findings"], [])
                    self.assertEqual(out["summary"], finished["summary"])
                    self.assertEqual(out["review_markdown"], finished["review_markdown"])
                    self.assertNotEqual(out, stale)
                    self.assertNotEqual(out.get("findings"), stale["findings"])

    def test_finished_empty_critic_structured_output_beats_stale_text_issues(self):
        schema = _load_schema("critic.json")
        finished = {
            "accepts": True,
            "issues": [],
            "critic_markdown": "No issues.",
            "attacks": [],
        }
        stale = {
            "accepts": False,
            "issues": ["phantom leftover draft"],
            "critic_markdown": "stale critic",
            "attacks": [{"question": "q", "lands": True}],
        }
        envelope = {
            "structuredOutput": finished,
            "text": json.dumps(stale),
            "sessionId": "abc",
        }
        out = extract_json(json.dumps(envelope), schema=schema)
        self.assertIs(out["accepts"], True)
        self.assertEqual(out["issues"], [])
        self.assertNotIn("phantom leftover draft", json.dumps(out))
        self.assertEqual(out["critic_markdown"], finished["critic_markdown"])

    def test_trailing_finished_empty_in_text_beats_prior_complete(self):
        schema = _load_schema("review.json")
        finished = {
            "findings": [],
            "summary": "No issues.",
            "review_markdown": "No issues.",
        }
        cases = (
            json.dumps(_REVIEW) + json.dumps(finished),
            json.dumps(_REVIEW) + json.dumps(finished) + "<|eos|>",
            json.dumps(
                {
                    "text": json.dumps(_REVIEW) + json.dumps(finished),
                    "sessionId": "abc",
                }
            ),
            json.dumps(
                {
                    "text": json.dumps(_REVIEW) + json.dumps(finished) + "<|eos|>",
                    "sessionId": "abc",
                }
            ),
        )
        for raw in cases:
            with self.subTest(raw=raw[:50]):
                out = extract_json(raw, schema=schema)
                self.assertEqual(out["findings"], [])
                self.assertEqual(out["summary"], finished["summary"])

    def test_incomplete_empty_structured_output_does_not_beat_complete_text(self):
        schema = _load_schema("review.json")
        incompletes = (
            {"findings": []},
            {},
            _FINDING,
        )
        for incomplete in incompletes:
            for structured in (incomplete, json.dumps(incomplete)):
                with self.subTest(incomplete=incomplete, kind=type(structured).__name__):
                    envelope = {
                        "structuredOutput": structured,
                        "text": json.dumps(_REVIEW),
                        "sessionId": "abc",
                    }
                    out = extract_json(json.dumps(envelope), schema=schema)
                    self._assert_review(out)

    def test_incomplete_result_and_snake_case_wrapper_do_not_beat_complete_text(self):
        incomplete = {"findings": []}
        cases = (
            {"result": incomplete, "text": json.dumps(_REVIEW)},
            {"result": json.dumps(incomplete), "text": json.dumps(_REVIEW)},
            {"structured_output": incomplete, "text": json.dumps(_REVIEW)},
            {"structured_output": json.dumps(incomplete), "text": json.dumps(_REVIEW)},
        )
        for envelope in cases:
            with self.subTest(keys=sorted(envelope)):
                envelope = dict(envelope, sessionId="abc")
                out = extract_json(json.dumps(envelope), schema=_REVIEW_SCHEMA)
                self._assert_review(out)

    def test_complete_wrapper_still_unwraps_when_text_is_vacuous(self):
        incomplete = {"findings": []}
        cases = (
            {"structuredOutput": _REVIEW, "text": json.dumps(incomplete)},
            {"structuredOutput": _REVIEW, "text": ""},
            {"result": _REVIEW, "text": json.dumps(incomplete)},
        )
        for envelope in cases:
            with self.subTest(keys=sorted(k for k in envelope if envelope[k] != "")):
                envelope = dict(envelope, sessionId="abc")
                out = extract_json(json.dumps(envelope), schema=_REVIEW_SCHEMA)
                self._assert_review(out)

    def test_trailing_incomplete_object_does_not_wipe_prior_complete(self):
        incompletes = ({"findings": []}, {})
        streams = []
        for incomplete in incompletes:
            streams.extend(
                (
                    json.dumps(_REVIEW) + json.dumps(incomplete) + "<|eos|>",
                    json.dumps(_REVIEW) + json.dumps(incomplete),
                    json.dumps(
                        {
                            "text": json.dumps(_REVIEW)
                            + json.dumps(incomplete)
                            + "<|eos|>",
                            "sessionId": "abc",
                        }
                    ),
                )
            )
        for raw in streams:
            with self.subTest(raw=raw[:40]):
                out = extract_json(raw, schema=_REVIEW_SCHEMA)
                self._assert_review(out)

    def test_placeholder_then_complete_in_text_still_keeps_the_complete(self):
        first = {"summary": "investigating", "findings": []}
        raw = json.dumps({"text": json.dumps(first) + json.dumps(_REVIEW)})
        out = extract_json(raw, schema=_REVIEW_SCHEMA)
        self._assert_review(out)

    def test_finding_shaped_structured_output_is_still_skipped(self):
        envelope = {
            "structuredOutput": _FINDING,
            "text": json.dumps(_REVIEW) + "<|eos|>",
            "sessionId": "abc",
        }
        out = extract_json(json.dumps(envelope), schema=_REVIEW_SCHEMA)
        self._assert_review(out)

    def test_schema_skips_finding_shaped_structured_output(self):
        envelope = {
            "structuredOutput": _FINDING,
            "text": json.dumps(_REVIEW) + "<|eos|>",
            "sessionId": "abc",
        }
        out = extract_json(json.dumps(envelope), schema=_REVIEW_SCHEMA)
        self.assertEqual(out["summary"], _REVIEW["summary"])
        self.assertEqual(len(out["findings"]), 2)

    def test_finished_empty_wrapper_rule_holds_for_every_array_schema(self):
        named = []
        for name, schema in _array_schemas():
            named.append(name)
            finished = _finished_empty(schema)
            complete = _complete_payload(schema)
            arrays = _top_level_arrays(schema)
            required = list(schema.get("required") or [])
            envelope = {
                "structuredOutput": finished,
                "text": json.dumps(complete),
                "sessionId": "abc",
            }
            out = extract_json(json.dumps(envelope), schema=schema)
            self.assertNotEqual(out, complete, name)
            for key in arrays:
                self.assertEqual(
                    out.get(key),
                    [],
                    "%s: finished empty %s lost to leftover draft: %s"
                    % (name, key, out),
                )
            for key in required:
                self.assertEqual(
                    out.get(key),
                    finished.get(key),
                    "%s: required %s did not match finished empty: %s"
                    % (name, key, out),
                )
        self.assertIn("review.json", named)
        self.assertIn("guardian.json", named)
        self.assertIn("critic.json", named)

    def test_incomplete_wrapper_still_loses_for_every_array_schema(self):
        named = []
        for name, schema in _array_schemas():
            named.append(name)
            incomplete = _incomplete_payload(schema)
            complete = _complete_payload(schema)
            arrays = _top_level_arrays(schema)
            envelope = {
                "structuredOutput": incomplete,
                "text": json.dumps(complete),
                "sessionId": "abc",
            }
            out = extract_json(json.dumps(envelope), schema=schema)
            for key in arrays:
                self.assertTrue(
                    out.get(key),
                    "%s: incomplete wrapper wiped complete %s: %s" % (name, key, out),
                )
        self.assertIn("review.json", named)
        self.assertIn("guardian.json", named)
        self.assertIn("critic.json", named)

    def test_structured_output_json_string_is_not_the_only_envelope(self):
        src = Path(__file__).read_text(encoding="utf-8")
        self.assertIn(
            "test_finished_empty_review_structured_output_beats_stale_text_findings",
            src,
        )
        self.assertIn("test_incomplete_empty_structured_output_does_not_beat_complete_text", src)
        self.assertIn('"structuredOutput"', src)
        self.assertIn('"structured_output"', src)
        self.assertIn("json.dumps(_REVIEW)", src)


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
