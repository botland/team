import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from team import style
from team.findings import (
    FindingsError,
    collect_guardian_findings,
    collect_review_findings,
    empty_seq_state,
    finding_id,
    format_chain,
    format_console_lines,
    group_by_kind,
    latest_seq_rows,
    mark_seq_step,
    needs_classify,
    normalize_kind,
    pick_next_seq,
    related_guardian,
    render_followups,
    render_seq_log,
    reopen_prefix,
    seq_candidates,
    seq_status_for,
    take_important,
)
from team.util import dump_json


# Closed guardian link → kind map from the apply contract (not imported from impl).
_LINK_KIND = {
    "r_to_a": "architecture",
    "a_to_t": "test",
    "t_to_i": "implementation",
    "i_to_r": "architecture",
    "invariant": "architecture",
}
_CHAIN_KEYS = ("r_to_a", "a_to_t", "t_to_i", "i_to_r")


def _item(kind, title, path="src/a.py", source="reviewer-fake", severity="high"):
    return {
        "kind": kind,
        "severity": severity,
        "title": title,
        "path": path,
        "source": source,
    }


def _with_id(item):
    row = dict(item)
    row["id"] = finding_id(row)
    return row


def _chain(*failed):
    return {
        key: {"ok": key not in failed, "note": "n"}
        for key in _CHAIN_KEYS
    }


def _write_guardian(work: Path, risks=None, chain=None):
    (work / "prompts").mkdir(exist_ok=True)
    payload = {"risks": list(risks or [])}
    if chain is not None:
        payload["chain"] = chain
    dump_json(work / "prompts" / "guardian.result.json", payload)


def _candidate_ids(findings, seq):
    return [row["id"] for row in seq_candidates(findings, seq)]


def _assert_sets_disjoint(test, seq):
    applied = set(seq.get("applied") or [])
    skipped = set(seq.get("skipped") or [])
    stale = set(seq.get("stale") or [])
    test.assertEqual(applied & skipped, set())
    test.assertEqual(applied & stale, set())
    test.assertEqual(skipped & stale, set())
    failed = seq.get("failed") or ""
    resume = seq.get("resume") or ""
    if failed:
        test.assertNotIn(failed, applied)
        test.assertNotIn(failed, skipped)
        test.assertNotIn(failed, stale)
    if resume:
        test.assertNotIn(resume, applied)
        test.assertNotIn(resume, skipped)
        test.assertNotIn(resume, stale)
        test.assertNotEqual(resume, failed)


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

    def test_console_lines_color_tags_and_stay_plain_when_disabled(self):
        rows = [
            {
                "severity": "high",
                "kind": "architecture",
                "title": "[i_to_r] living design",
                "path": "src/team/pipeline.py",
                "evidence": "design.md replaced",
            }
        ]
        plain = format_console_lines(rows, color=False)
        colored = format_console_lines(rows, color=True)
        self.assertEqual(len(plain), len(colored))
        self.assertTrue(plain[0].startswith("  1. [high/architecture]"))
        self.assertIn("[i_to_r] living design", plain[0])
        self.assertNotIn("\033", "\n".join(plain))
        self.assertIn("\033", colored[0])
        self.assertIn(style.RED, colored[0])
        self.assertIn(style.BRIGHT_BLUE, colored[0])
        self.assertIn(style.BRIGHT_MAGENTA, colored[0])
        self.assertEqual(style.strip_ansi(colored[0]), plain[0])
        self.assertEqual(style.strip_ansi(colored[1]), plain[1])

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
            by_link = {}
            for row in rows:
                for link, kind in _LINK_KIND.items():
                    if link in row["title"]:
                        by_link[link] = row
                        self.assertEqual(row["kind"], kind)
            self.assertEqual(by_link["i_to_r"]["kind"], "architecture")
            self.assertEqual(by_link["a_to_t"]["kind"], "test")

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
        item = {"path": "src/a.py", "title": "x", "kind": "test", "source": "reviewer-fake"}
        same = {
            "path": "src/a.py",
            "title": "[t_to_i] same",
            "kind": "implementation",
            "source": "guardian",
        }
        other = {
            "path": "src/b.py",
            "title": "other",
            "kind": "implementation",
            "source": "guardian",
        }
        related = related_guardian(item, [same, other])
        self.assertEqual([r["title"] for r in related], ["[t_to_i] same"])
        self.assertEqual(related_guardian({"path": "", "title": "x"}, [same, other]), [])
        # Path filter is not the apply contract. Marking the test primary applied
        # must leave the same-path t_to_i pending (context only, never hops=related).
        seq = mark_seq_step(
            empty_seq_state(),
            _with_id(item),
            status="applied",
            hops=["tdd-design contract", "test-writer", "suite PASS"],
        )
        self.assertNotIn(finding_id(same), seq["applied"])
        self.assertNotIn(["related"], [step.get("hops") for step in seq["steps"]])
        ids = _candidate_ids([item, same, other], seq)
        self.assertIn(finding_id(same), ids)
        self.assertNotIn(finding_id(item), ids)
        self.assertNotIn(finding_id(other), seq["applied"])

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
        ids = [row["id"] for row in seq_candidates([arch, impl], seq)]
        self.assertEqual(ids, [finding_id(arch)])
        guardian = {
            "kind": "architecture",
            "severity": "invariant",
            "title": "[i_to_r] slash taught as legal",
            "path": "src/b.py",
            "source": "guardian",
        }
        empty = {"applied": [], "skipped": [], "stale": [], "failed": "", "resume": "", "steps": []}
        self.assertEqual(
            [row["source"] for row in seq_candidates([arch, guardian], empty)],
            ["reviewer-fake", "guardian"],
        )
        rows = latest_seq_rows(seq)
        by_id = {r["id"]: r["status"] for r in rows}
        self.assertEqual(by_id[finding_id(arch)], "reopened")
        self.assertEqual(by_id[finding_id(impl)], "stale")
        # Restore half: prefix applied returns the suffix to pending (not stale, not applied).
        seq = mark_seq_step(seq, dict(arch, id=finding_id(arch)), status="applied")
        _assert_sets_disjoint(self, seq)
        self.assertNotIn(finding_id(impl), seq["stale"])
        self.assertNotIn(finding_id(impl), seq["applied"])
        self.assertNotIn(finding_id(impl), seq["skipped"])
        ids = _candidate_ids([arch, impl], seq)
        self.assertIn(finding_id(impl), ids)
        self.assertIn(finding_id(arch), seq["applied"])
        self.assertNotIn(finding_id(arch), ids)
        self.assertTrue(set(seq.get("stale") or []).isdisjoint(ids))
        by_id = {r["id"]: r["status"] for r in latest_seq_rows(seq)}
        self.assertEqual(by_id[finding_id(arch)], "applied")
        self.assertIn(by_id[finding_id(impl)], ("", "pending"))
        self.assertNotEqual(seq_status_for(impl, seq), "stale")

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

        with self.assertRaises(FindingsError):
            reopen_prefix(seq, finding_id(item))

    def test_stale_suffix_becomes_candidate_after_prefix_applied(self):
        arch = _item("architecture", "one egress")
        impl = _item("implementation", "credential")
        pair = [arch, impl]
        seq = empty_seq_state()
        seq = mark_seq_step(seq, _with_id(arch), status="applied")
        seq = mark_seq_step(seq, _with_id(impl), status="applied")
        seq = reopen_prefix(seq, finding_id(arch))
        self.assertEqual(_candidate_ids(pair, seq), [finding_id(arch)])
        self.assertIn(finding_id(impl), seq["stale"])
        seq = mark_seq_step(seq, _with_id(arch), status="applied")
        _assert_sets_disjoint(self, seq)
        self.assertNotIn(finding_id(impl), seq["stale"])
        self.assertNotIn(finding_id(impl), seq["applied"])
        self.assertNotIn(finding_id(impl), seq["skipped"])
        ids = _candidate_ids(pair, seq)
        self.assertIn(finding_id(impl), ids)
        self.assertIn(finding_id(arch), seq["applied"])
        self.assertNotIn(finding_id(arch), ids)
        self.assertTrue(set(seq.get("stale") or []).isdisjoint(ids))
        displayed = {r["id"]: r["status"] for r in latest_seq_rows(seq)}
        self.assertIn(displayed[finding_id(impl)], ("", "pending"))
        # Second reopen after the suffix was restored and applied again.
        seq = mark_seq_step(seq, _with_id(impl), status="applied")
        seq = reopen_prefix(seq, finding_id(arch))
        self.assertIn(finding_id(impl), seq["stale"])
        self.assertEqual(_candidate_ids(pair, seq), [finding_id(arch)])

    def test_stale_suffix_stays_excluded_while_prefix_failed(self):
        arch = _item("architecture", "one egress")
        impl = _item("implementation", "credential")
        pair = [arch, impl]
        seq = empty_seq_state()
        seq = mark_seq_step(seq, _with_id(arch), status="applied")
        seq = mark_seq_step(seq, _with_id(impl), status="applied")
        seq = reopen_prefix(seq, finding_id(arch))
        seq = mark_seq_step(seq, _with_id(arch), status="failed", suite="FAIL")
        _assert_sets_disjoint(self, seq)
        self.assertIn(finding_id(impl), seq["stale"])
        self.assertEqual(seq["failed"], finding_id(arch))
        ids = _candidate_ids(pair, seq)
        self.assertEqual(ids, [finding_id(arch)])
        self.assertNotIn(finding_id(impl), ids)
        self.assertTrue(set(seq.get("stale") or []).isdisjoint(ids))
        self.assertEqual(pick_next_seq(pair, seq)["id"], finding_id(arch))
        displayed = {r["id"]: r["status"] for r in latest_seq_rows(seq)}
        self.assertEqual(displayed[finding_id(impl)], "stale")
        self.assertEqual(displayed[finding_id(arch)], "failed")

    def test_stale_suffix_becomes_candidate_after_prefix_skip_failed(self):
        arch = _item("architecture", "one egress")
        impl = _item("implementation", "credential")
        pair = [arch, impl]
        seq = empty_seq_state()
        seq = mark_seq_step(seq, _with_id(arch), status="applied")
        seq = mark_seq_step(seq, _with_id(impl), status="applied")
        seq = reopen_prefix(seq, finding_id(arch))
        seq = mark_seq_step(seq, _with_id(arch), status="failed", suite="FAIL")
        seq = mark_seq_step(seq, _with_id(arch), status="skipped")
        _assert_sets_disjoint(self, seq)
        self.assertIn(finding_id(arch), seq["skipped"])
        self.assertNotIn(finding_id(impl), seq["stale"])
        self.assertNotIn(finding_id(impl), seq["applied"])
        self.assertNotIn(finding_id(impl), seq["skipped"])
        ids = _candidate_ids(pair, seq)
        self.assertIn(finding_id(impl), ids)
        self.assertNotIn(finding_id(arch), ids)
        self.assertTrue(set(seq.get("stale") or []).isdisjoint(ids))
        displayed = {r["id"]: r["status"] for r in latest_seq_rows(seq)}
        self.assertEqual(displayed[finding_id(arch)], "skipped")
        self.assertIn(displayed[finding_id(impl)], ("", "pending"))
        with self.assertRaises(FindingsError):
            reopen_prefix(seq, finding_id(arch))

    def test_reopen_rejects_stale(self):
        arch = _item("architecture", "one egress")
        impl = _item("implementation", "credential")
        seq = empty_seq_state()
        seq = mark_seq_step(seq, _with_id(arch), status="applied")
        seq = mark_seq_step(seq, _with_id(impl), status="applied")
        seq = reopen_prefix(seq, finding_id(arch))
        self.assertEqual(seq_status_for(impl, seq), "stale")
        with self.assertRaises(FindingsError) as ctx:
            reopen_prefix(seq, finding_id(impl))
        self.assertIn("stale", str(ctx.exception).lower())
        with self.assertRaises(FindingsError):
            reopen_prefix(seq, finding_id(arch))
        with self.assertRaises(FindingsError):
            reopen_prefix(seq, "deadbeefcafe")

    def test_list_and_followups_distinguish_stale_from_skipped(self):
        arch = _item("architecture", "one egress")
        test = _item("test", "missing reject")
        impl = _item("implementation", "credential")
        seq = empty_seq_state()
        seq = mark_seq_step(seq, _with_id(arch), status="skipped")
        seq = mark_seq_step(seq, _with_id(test), status="applied")
        seq = mark_seq_step(seq, _with_id(impl), status="applied")
        seq = reopen_prefix(seq, finding_id(test))
        _assert_sets_disjoint(self, seq)
        self.assertEqual(seq_status_for(arch, seq), "skipped")
        self.assertEqual(seq_status_for(test, seq), "reopened")
        self.assertEqual(seq_status_for(impl, seq), "stale")
        self.assertNotEqual(seq_status_for(arch, seq), seq_status_for(impl, seq))
        md = render_followups([arch, test, impl], seq=seq)
        self.assertIn("**skipped**", md)
        self.assertIn("**stale**", md)
        self.assertNotIn("**done**", md)
        self.assertNotIn("**closed**", md)
        self.assertNotIn("**excluded**", md)
        by_id = {r["id"]: r["status"] for r in latest_seq_rows(seq)}
        self.assertEqual(by_id[finding_id(arch)], "skipped")
        self.assertEqual(by_id[finding_id(impl)], "stale")
        log = render_seq_log(seq, slug="demo")
        self.assertIn("stale, not skipped", log)
        self.assertIn("## Stale", log)
        self.assertIn(finding_id(impl), log)
        seq = mark_seq_step(seq, _with_id(test), status="applied")
        md_after = render_followups([arch, test, impl], seq=seq)
        self.assertIn("**skipped**", md_after)
        self.assertNotIn("**stale**", md_after)
        after = {r["id"]: r["status"] for r in latest_seq_rows(seq)}
        self.assertEqual(after[finding_id(arch)], "skipped")
        self.assertIn(after[finding_id(impl)], ("", "pending"))

    def test_failed_chain_without_risks_emits_synthetic_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            failed = _CHAIN_KEYS
            _write_guardian(work, risks=[], chain=_chain(*failed))
            rows = collect_guardian_findings(work)
            self.assertTrue(rows, "failed chain cells must yield queue rows, not []")
            present = {key for key in failed for row in rows if key in row["title"]}
            self.assertEqual(present, set(failed))
            for key in failed:
                matching = [row for row in rows if key in row["title"]]
                self.assertEqual(len(matching), 1, key)
                row = matching[0]
                self.assertEqual(row["source"], "guardian")
                self.assertEqual(row["severity"], "invariant")
                self.assertEqual(row["kind"], _LINK_KIND[key])
                self.assertFalse(row.get("path"))
            kinds = {row["kind"] for row in rows}
            self.assertEqual(kinds, {_LINK_KIND[key] for key in failed})
            self.assertEqual(
                related_guardian({"path": "src/a.py", "title": "x"}, rows),
                [],
            )

            _write_guardian(work, risks=[], chain=_chain())
            self.assertEqual(collect_guardian_findings(work), [])

            risk = {
                "title": "slash still taught as legal",
                "evidence": "error names slash",
                "path": "README",
                "link": "t_to_i",
            }
            _write_guardian(work, risks=[risk], chain=_chain("t_to_i"))
            covered = collect_guardian_findings(work)
            t_to_i_rows = [row for row in covered if "t_to_i" in row["title"]]
            self.assertEqual(len(t_to_i_rows), 1)
            self.assertEqual(t_to_i_rows[0]["kind"], "implementation")
            self.assertIn("slash still taught as legal", t_to_i_rows[0]["title"])

            _write_guardian(
                work,
                risks=[
                    {
                        "title": "tree missed the brief",
                        "evidence": "invariant",
                        "path": "src/a.py",
                        "link": "invariant",
                    }
                ],
                chain=_chain("t_to_i", "i_to_r"),
            )
            invariant_rows = collect_guardian_findings(work)
            self.assertTrue(any(row["kind"] == "architecture" for row in invariant_rows))
            self.assertFalse(
                any(
                    "t_to_i" in row["title"] and row["kind"] == "implementation"
                    for row in invariant_rows
                )
            )

            _write_guardian(
                work,
                risks=[
                    {
                        "title": "contract hole",
                        "evidence": "no reject case",
                        "path": "tests/t.py",
                        "link": "a_to_t",
                    }
                ],
                chain=_chain("t_to_i"),
            )
            mixed = collect_guardian_findings(work)
            self.assertTrue(
                any("a_to_t" in row["title"] and "contract hole" in row["title"] for row in mixed)
            )
            self.assertTrue(
                any("t_to_i" in row["title"] and row["kind"] == "implementation" for row in mixed)
            )

    def test_unknown_guardian_link_is_unclassified_not_architecture(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            _write_guardian(
                work,
                risks=[
                    {
                        "title": "empty link",
                        "evidence": "no link",
                        "path": "src/a.py",
                        "link": "",
                    },
                    {
                        "title": "typo link",
                        "evidence": "t2i",
                        "path": "src/b.py",
                        "link": "t2i",
                    },
                    {
                        "title": "omitted link",
                        "evidence": "missing",
                        "path": "src/c.py",
                    },
                    {
                        "title": "kind side channel",
                        "evidence": "kind must not win",
                        "path": "src/d.py",
                        "link": "t_to_i",
                        "kind": "architecture",
                    },
                    {
                        "title": "normalized closed key",
                        "evidence": "T_TO_I lowercases to t_to_i",
                        "path": "src/e.py",
                        "link": "T_TO_I",
                    },
                    {
                        "title": "i_to_r stays architecture",
                        "evidence": "not implementer-only",
                        "path": "src/f.py",
                        "link": "i_to_r",
                    },
                    {
                        "title": "mystery link",
                        "evidence": "mystery",
                        "path": "src/g.py",
                        "link": "mystery",
                    },
                    {
                        "title": "kind back door",
                        "evidence": "impl side channel",
                        "path": "src/h.py",
                        "link": "t2i",
                        "kind": "implementation",
                    },
                ],
                chain=_chain(),
            )
            rows = collect_guardian_findings(work)
            by_evidence = {row["evidence"]: row for row in rows}
            for key in ("no link", "t2i", "missing", "mystery", "impl side channel"):
                self.assertEqual(by_evidence[key]["kind"], "unclassified")
                self.assertNotEqual(by_evidence[key]["kind"], "architecture")
            self.assertTrue(needs_classify(rows))
            self.assertEqual(by_evidence["kind must not win"]["kind"], "implementation")
            self.assertEqual(
                by_evidence["T_TO_I lowercases to t_to_i"]["kind"], "implementation"
            )
            self.assertEqual(
                by_evidence["not implementer-only"]["kind"], "architecture"
            )
            self.assertNotEqual(
                by_evidence["not implementer-only"]["kind"], "implementation"
            )
            cands = {row["id"] for row in seq_candidates(rows, empty_seq_state())}
            for row in rows:
                if row["kind"] == "unclassified":
                    self.assertNotIn(finding_id(row), cands)
            self.assertIn(finding_id(by_evidence["kind must not win"]), cands)
            self.assertIn(finding_id(by_evidence["not implementer-only"]), cands)


if __name__ == "__main__":
    unittest.main()
