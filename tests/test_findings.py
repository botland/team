import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team.findings import (
    collect_guardian_findings,
    collect_review_findings,
    finding_id,
    format_chain,
    format_console_lines,
    group_by_kind,
    mark_seq_step,
    needs_classify,
    normalize_kind,
    pick_next_seq,
    related_guardian,
    render_followups,
    reopen_prefix,
    latest_seq_rows,
    take_important,
)
from team.util import dump_json


class FindingsTests(unittest.TestCase):
    def test_normalize_kind(self):
        self.assertEqual(normalize_kind("implementation"), "implementation")
        self.assertEqual(normalize_kind("IMPL"), "implementation")
        self.assertEqual(normalize_kind("tdd-design"), "test")
        self.assertEqual(normalize_kind("architect"), "architecture")
        self.assertEqual(normalize_kind("mystery"), "unclassified")

    def test_needs_classify(self):
        self.assertFalse(needs_classify([]))
        self.assertFalse(
            needs_classify([{"kind": "test", "title": "x"}])
        )
        self.assertTrue(
            needs_classify([{"kind": "", "title": "x"}, {"kind": "test", "title": "y"}])
        )

    def test_needs_classify_markdown_only_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "review.md").write_text("# Review\n\nbug\n", encoding="utf-8")
            self.assertTrue(needs_classify([], work=work))
            (work / "prompts").mkdir()
            dump_json(work / "prompts" / "reviewer-grok.result.json", {"findings": []})
            self.assertFalse(needs_classify([], work=work))

    def test_group_and_followups(self):
        rows = [
            {"kind": "implementation", "severity": "high", "title": "off-by-one", "path": "src/a.py"},
            {"kind": "note", "severity": "low", "title": "open class", "path": ""},
        ]
        groups = group_by_kind(rows)
        self.assertEqual(len(groups["implementation"]), 1)
        self.assertEqual(len(groups["note"]), 1)
        md = render_followups(rows)
        self.assertIn("[implementation]", md)
        self.assertIn("off-by-one", md)

    def test_collect_dedupes_same_path_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            prompts = work / "prompts"
            prompts.mkdir()
            finding = {
                "severity": "high",
                "title": "leak",
                "evidence": "x",
                "path": "src/a.py",
                "kind": "implementation",
            }
            dump_json(prompts / "reviewer-claude.result.json", {"findings": [finding]})
            dump_json(prompts / "reviewer-grok.result.json", {"findings": [finding]})
            found = collect_review_findings(work)
            self.assertEqual(len(found), 1)

    def test_console_lines_cap_and_rank(self):
        rows = [
            {"severity": "low", "kind": "note", "title": "n%d" % i, "path": "", "evidence": ""}
            for i in range(12)
        ]
        rows[3] = {
            "severity": "high",
            "kind": "implementation",
            "title": "real bug",
            "path": "src/a.py",
            "evidence": "off by one",
        }
        lines = format_console_lines(rows, sort=True, more_hint="apply-plan.md")
        joined = "\n".join(lines)
        self.assertIn("real bug", joined)
        self.assertIn("src/a.py", joined)
        self.assertIn("off by one", joined)
        self.assertIn("+2 more in apply-plan.md", joined)
        self.assertLessEqual(
            sum(1 for ln in lines if ln.startswith("  ") and ln[2:3].isdigit()), 10
        )
        top = take_important(rows, 1)
        self.assertEqual(top[0]["title"], "real bug")

    def test_console_empty_is_silent(self):
        self.assertEqual(format_console_lines([]), [])

    def test_format_chain_and_guardian_link_kind(self):
        self.assertIn("I→R fail", format_chain({
            "r_to_a": {"ok": True, "note": "x"},
            "a_to_t": {"ok": True, "note": "x"},
            "t_to_i": {"ok": True, "note": "x"},
            "i_to_r": {"ok": False, "note": "missed brief"},
        }))
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "prompts").mkdir()
            dump_json(
                work / "prompts" / "guardian.result.json",
                {
                    "risks": [
                        {
                            "title": "brief not shipped",
                            "evidence": "no handler",
                            "path": "src/a.py",
                            "link": "i_to_r",
                        },
                        {
                            "title": "contract hole",
                            "evidence": "no reject case",
                            "path": "tests/t.py",
                            "link": "a_to_t",
                        },
                    ]
                },
            )
            rows = collect_guardian_findings(work)
            kinds = {r["kind"] for r in rows}
            self.assertIn("architecture", kinds)
            self.assertIn("test", kinds)
            self.assertTrue(any("i_to_r" in r["title"] for r in rows))

    def test_seq_picks_arch_before_impl_even_if_impl_is_higher_severity(self):
        impl = {
            "kind": "implementation",
            "severity": "high",
            "title": "credential missing",
            "path": "src/a.py",
            "source": "reviewer-grok",
        }
        arch = {
            "kind": "architecture",
            "severity": "low",
            "title": "one egress",
            "path": "src/a.py",
            "source": "reviewer-grok",
        }
        nxt = pick_next_seq([arch, impl], {"applied": [], "skipped": [], "failed": "", "steps": []})
        self.assertEqual(nxt["title"], "one egress")
        self.assertEqual(nxt["id"], finding_id(arch))

    def test_seq_severity_breaks_ties_inside_a_kind(self):
        high = {
            "kind": "architecture",
            "severity": "high",
            "title": "cache key",
            "path": "src/a.py",
            "source": "reviewer-grok",
        }
        medium = {
            "kind": "architecture",
            "severity": "medium",
            "title": "one egress",
            "path": "src/b.py",
            "source": "reviewer-grok",
        }
        nxt = pick_next_seq([medium, high], {"applied": [], "skipped": [], "failed": "", "steps": []})
        self.assertEqual(nxt["title"], "cache key")

    def test_seq_retries_failed_before_next(self):
        impl = {
            "kind": "implementation",
            "severity": "high",
            "title": "a",
            "path": "src/a.py",
            "source": "reviewer-fake",
        }
        test = {
            "kind": "test",
            "severity": "high",
            "title": "b",
            "path": "tests/t.py",
            "source": "reviewer-fake",
        }
        seq = mark_seq_step(
            {"applied": [], "skipped": [], "failed": "", "steps": []},
            dict(impl, id=finding_id(impl)),
            status="failed",
            suite="FAIL",
        )
        nxt = pick_next_seq([impl, test], seq)
        self.assertEqual(nxt["title"], "a")

    def test_related_guardian_same_path_only(self):
        item = {"path": "src/a.py", "title": "x"}
        related = related_guardian(
            item,
            [
                {"path": "src/a.py", "title": "same"},
                {"path": "src/b.py", "title": "other"},
            ],
        )
        self.assertEqual([r["title"] for r in related], ["same"])

    def test_followups_marks_applied(self):
        item = {
            "kind": "implementation",
            "severity": "high",
            "title": "bug",
            "path": "src/a.py",
            "source": "reviewer-fake",
        }
        seq = mark_seq_step(
            {"applied": [], "skipped": [], "failed": "", "steps": []},
            dict(item, id=finding_id(item)),
            status="applied",
        )
        md = render_followups([item], seq=seq)
        self.assertIn("**applied**", md)

    def test_reopen_marks_later_stale_and_resumes(self):
        arch = {
            "kind": "architecture",
            "severity": "high",
            "title": "one egress",
            "path": "src/a.py",
            "source": "reviewer-fake",
        }
        impl = {
            "kind": "implementation",
            "severity": "high",
            "title": "credential",
            "path": "src/a.py",
            "source": "reviewer-fake",
        }
        seq = {"applied": [], "skipped": [], "stale": [], "failed": "", "resume": "", "steps": []}
        seq = mark_seq_step(seq, dict(arch, id=finding_id(arch)), status="applied")
        seq = mark_seq_step(seq, dict(impl, id=finding_id(impl)), status="failed", suite="FAIL")
        seq = reopen_prefix(seq, finding_id(arch))
        self.assertEqual(seq["resume"], finding_id(arch))
        self.assertIn(finding_id(impl), seq["stale"])
        self.assertNotIn(finding_id(arch), seq["applied"])
        nxt = pick_next_seq([arch, impl], seq)
        self.assertEqual(nxt["id"], finding_id(arch))
        from team.findings import seq_candidates

        ids = [row["id"] for row in seq_candidates([arch, impl], seq)]
        self.assertEqual(ids, [finding_id(arch)])
        rows = latest_seq_rows(seq)
        by_id = {r["id"]: r["status"] for r in rows}
        self.assertEqual(by_id[finding_id(arch)], "reopened")
        self.assertEqual(by_id[finding_id(impl)], "stale")

    def test_reopen_rejects_skipped(self):
        item = {
            "kind": "implementation",
            "title": "x",
            "path": "src/a.py",
            "source": "reviewer-fake",
        }
        seq = mark_seq_step(
            {"applied": [], "skipped": [], "stale": [], "failed": "", "resume": "", "steps": []},
            dict(item, id=finding_id(item)),
            status="skipped",
        )
        from team.findings import FindingsError

        with self.assertRaises(FindingsError):
            reopen_prefix(seq, finding_id(item))


if __name__ == "__main__":
    unittest.main()
