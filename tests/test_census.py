"""Shared census.md: one tree inventory, first writer wins."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.config import load_config, schema_path
from team.pipeline import (
    CENSUS_ARTIFACT,
    CROSS_ROLE_ARTIFACTS,
    INLINE_ARTIFACT_MAX,
    INLINE_TOTAL_MAX,
    PipelineError,
    start_feature,
    start_range_review,
)
from team.schemas import validate as validate_schema
from team.util import load_json
from tests.support.hostile import emit, register_runtime
from tests.support.hostile import HostileRuntime
from team.gitutil import product_paths
from tests.support.repo import git, init_repo


class CensusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def _pipe(self):
        cfg = load_config(self.repo, fake=True)
        cfg.code_root = "src"
        cfg.test_root = "tests"
        return start_feature(cfg, "Add greet helper", "census-demo")

    def test_first_inspect_hop_writes_census_later_hops_do_not_overwrite(self):
        pipe = self._pipe()
        first = HostileRuntime(
            [
                emit(
                    {
                        "design_markdown": "# Design\n",
                        "code_root": "src",
                        "test_root": "tests",
                        "census_markdown": "# Census\n\nfirst writer\n",
                    }
                )
            ],
            phases=("architect",),
            num_turns=2,
        )
        second = HostileRuntime(
            [
                emit(
                    {
                        "accepts": True,
                        "issues": [],
                        "attacks": [],
                        "critic_markdown": "ok",
                        "census_markdown": "# Census\n\nsecond writer must not land\n",
                    }
                )
            ],
            phases=("critic",),
            num_turns=2,
        )
        with register_runtime("fake", first):
            pipe.invoke("architect", "architect", "design", "design.json")
        census = pipe.work / CENSUS_ARTIFACT
        self.assertTrue(census.is_file())
        self.assertIn("first writer", census.read_text(encoding="utf-8"))
        with register_runtime("fake", second):
            pipe.invoke("critic", "critic", "kill", "critic.json")
        self.assertIn("first writer", census.read_text(encoding="utf-8"))
        self.assertNotIn("second writer", census.read_text(encoding="utf-8"))

    def test_listed_artifacts_diet_and_recensus_rule(self):
        pipe = self._pipe()
        pipe.write_artifact("brief.md", "brief\n")
        text = pipe._listed_artifacts(["brief.md", "design.md", "review.md"])
        self.assertIn("brief.md", text)
        self.assertIn("Missing (n/a — do not open, do not invent):", text)
        self.assertIn("- design.md", text)
        self.assertNotIn("design.md (MISSING)", text)
        self.assertIn("census.md is missing", text)
        self.assertIn("emit census_markdown", text)
        self.assertNotIn("inspect the repository", text)

        pipe.write_artifact(CENSUS_ARTIFACT, "# Census\n\nlayout\n")
        later = pipe._listed_artifacts(["brief.md", "design.md"])
        self.assertIn("census.md is a map", later)
        self.assertIn("Do not recensus", later)
        self.assertIn("does not replace", later)
        # A census this small rides in the prompt rather than costing the hop a
        # tool round trip, but it is still present in full.
        self.assertIn("layout", later)
        self.assertIn("--- census.md (inlined", later)

    def test_census_does_not_remove_the_diff_from_required_reading(self):
        pipe = self._pipe()
        pipe.write_artifact("brief.md", "brief\n")
        dump = "x" * (80 * 1024)
        pipe.write_artifact("git/diff.patch", dump)
        first = pipe._listed_artifacts(["brief.md", "git/diff.patch"])
        self.assertIn("Read these files with tools before answering:", first)
        self.assertIn("git/diff.patch", first.split("Missing (n/a")[0])
        self.assertNotIn("Already inventoried", first)

        pipe.write_artifact(CENSUS_ARTIFACT, "# Census\n\nlayout\n")
        later = pipe._listed_artifacts(["brief.md", "git/diff.patch"])
        self.assertIn("census.md is a map", later)
        self.assertIn("does not replace", later)
        self.assertIn("git/diff.patch", later.split("Missing (n/a")[0])
        self.assertNotIn("Already inventoried", later)

    def test_range_reviewer_without_census_does_not_start_guardian(self):
        pipe = start_range_review(
            load_config(self.repo, fake=True, force=True), slug="no-census"
        )
        payload = {
            "summary": "ok",
            "findings": [
                {
                    "severity": "low",
                    "title": "n",
                    "evidence": "e",
                    "path": "README",
                    "kind": "note",
                }
            ],
            "review_markdown": "Finished review of the collected range.",
        }
        # census=False: the missing census *is* the subject here, so this double
        # must not get the seeding the others rely on.
        hostile = HostileRuntime(
            [emit(payload)],
            phases=("reviewer-fake",),
            num_turns=2,
            census=False,
        )
        with register_runtime("fake", hostile):
            with self.assertRaises(PipelineError) as ctx:
                pipe.phase_range_reviewer()
        self.assertIn("census", str(ctx.exception).lower())
        self.assertFalse((pipe.work / CENSUS_ARTIFACT).is_file())

    def test_consult_reads_census_and_does_not_ask_to_recensus(self):
        pipe = self._pipe()
        pipe.write_artifact("brief.md", "brief\n")
        pipe.write_artifact(CENSUS_ARTIFACT, "# Census\n\nlayout\n")
        pipe.consult("tdd-design", ["what is the seam?"], "architect")
        prompt = (pipe.work / "prompts" / "consult-001.prompt.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Do not recensus", prompt)
        self.assertIn("census.md is a map", prompt)
        self.assertNotIn("Read the work artifacts and the repo", prompt)

    def test_inspect_schemas_accept_optional_census_markdown(self):
        payloads = {
            "review.json": {"findings": [], "summary": "s", "census_markdown": "# Census\n"},
            "guardian.json": {
                "risks": [],
                "guardian_markdown": "g",
                "chain": {
                    "r_to_a": {"ok": None, "note": "n/a"},
                    "a_to_t": {"ok": None, "note": "n/a"},
                    "t_to_i": {"ok": None, "note": "n/a"},
                    "i_to_r": {"ok": False, "note": "n"},
                },
                "census_markdown": "# Census\n",
            },
            "design.json": {
                "design_markdown": "d",
                "code_root": ".",
                "test_root": "tests",
                "census_markdown": "# Census\n",
            },
            "tdd_design.json": {
                "ready": True,
                "questions": [],
                "test_contract_markdown": "t",
                "census_markdown": "# Census\n",
            },
            "answers.json": {"answers_markdown": "a", "census_markdown": "# Census\n"},
            "scout.json": {"components": [], "census_markdown": "# Census\n"},
        }
        for name, payload in payloads.items():
            schema = load_json(schema_path(name))
            self.assertIn("census_markdown", schema.get("properties") or {}, name)
            self.assertNotIn("census_markdown", schema.get("required") or [], name)
            errors = validate_schema(payload, schema, enums=False)
            self.assertEqual(errors, [], (name, errors))


if __name__ == "__main__":
    unittest.main()


class InlineScopeTests(unittest.TestCase):
    """What rides in the prompt, and what a hop must still go and fetch.

    Carrying a small input is cheaper than a tool round trip, which re-sends
    the hop's whole context. But a prompt is also a scope: whatever the
    orchestrator pastes in, the role has been handed. Artifacts that
    aggregate findings across roles are never pasted, at any size.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def _pipe(self, slug="inline-scope"):
        cfg = load_config(
            self.repo, fake=True, force=True, code_root="src", test_root="tests"
        )
        return start_feature(cfg, "brief", slug)

    def test_a_small_input_is_carried(self):
        pipe = self._pipe()
        pipe.write_artifact("design.md", "# Design\n\nsmall enough\n")
        text = pipe._listed_artifacts(["design.md"])
        self.assertIn("small enough", text)
        self.assertIn("do not open it again", text)

    def test_a_big_dump_stays_a_path(self):
        pipe = self._pipe()
        pipe.write_artifact("git/diff.patch", "x" * (80 * 1024))
        text = pipe._listed_artifacts(["git/diff.patch"])
        self.assertIn("Read these files with tools", text)
        self.assertIn(str(pipe.artifact("git/diff.patch")), text)
        self.assertNotIn("x" * 100, text)

    def test_cross_role_reports_are_never_carried(self):
        """A test-writer handed apply-plan.md has been handed every
        implementation finding in it. Scope is what the orchestrator gives."""
        pipe = self._pipe()
        for name in sorted(CROSS_ROLE_ARTIFACTS):
            pipe.write_artifact(name, "# %s\n\nfinding for another role\n" % name)
        text = pipe._listed_artifacts(sorted(CROSS_ROLE_ARTIFACTS))
        self.assertNotIn("finding for another role", text)
        for name in sorted(CROSS_ROLE_ARTIFACTS):
            self.assertIn(str(pipe.artifact(name)), text)

    def test_the_total_cap_holds_when_many_artifacts_are_small(self):
        pipe = self._pipe()
        names = []
        for i in range(12):
            name = "small-%02d.md" % i
            pipe.write_artifact(name, "y" * 3000)
            names.append(name)
        text = pipe._listed_artifacts(names)
        carried = text.count("inlined below")
        self.assertLessEqual(carried * 3000, INLINE_TOTAL_MAX)
        self.assertIn("Read these files with tools", text)

    def test_an_oversized_single_artifact_is_never_carried(self):
        pipe = self._pipe()
        pipe.write_artifact("design.md", "z" * (INLINE_ARTIFACT_MAX + 1))
        text = pipe._listed_artifacts(["design.md"])
        self.assertNotIn("z" * 100, text)
        self.assertIn(str(pipe.artifact("design.md")), text)


class CensusCacheTests(unittest.TestCase):
    """A census is a property of the commit, not of the slug that paid for it.

    Per-slug, every feature and every review bought the same tree map again.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def _pipe(self, slug):
        cfg = load_config(self.repo, fake=True, force=True)
        cfg.code_root = "src"
        cfg.test_root = "tests"
        return start_feature(cfg, "brief", slug)

    def _write_census(self, pipe, body="# Census\n\nlayout\n"):
        hostile = HostileRuntime(
            [
                emit(
                    {
                        "design_markdown": "# D\n",
                        "code_root": "src",
                        "test_root": "tests",
                        "census_markdown": body,
                    }
                )
            ],
            phases=("architect",),
            num_turns=2,
        )
        with register_runtime("fake", hostile):
            pipe.invoke("architect", "architect", "d", "design.json")

    def test_a_second_slug_at_the_same_head_does_not_buy_it_again(self):
        first = self._pipe("cache-one")
        self._write_census(first)
        self.assertTrue((first.work / CENSUS_ARTIFACT).is_file())
        second = self._pipe("cache-two")
        self.assertTrue(
            (second.work / CENSUS_ARTIFACT).is_file(),
            "a new slug at the same HEAD starts with the census already in hand",
        )
        self.assertIn("layout", (second.work / CENSUS_ARTIFACT).read_text(encoding="utf-8"))

    def test_a_new_commit_does_not_reuse_the_old_map(self):
        first = self._pipe("cache-head-one")
        self._write_census(first)
        (self.repo / "new.py").write_text("x = 1\n", encoding="utf-8")
        git(self.repo, "add", "new.py")
        git(self.repo, "commit", "-m", "move HEAD")
        second = self._pipe("cache-head-two")
        self.assertFalse(
            (second.work / CENSUS_ARTIFACT).is_file(),
            "the tree changed, so the cached map is not a map of it",
        )

    def test_a_reused_map_names_what_moved_under_it(self):
        first = self._pipe("cache-dirty-one")
        self._write_census(first)
        (self.repo / "scratch.py").write_text("y = 2\n", encoding="utf-8")
        second = self._pipe("cache-dirty-two")
        text = (second.work / CENSUS_ARTIFACT).read_text(encoding="utf-8")
        self.assertIn("Changed since this census", text)
        self.assertIn("scratch.py", text)
        self.assertIn("do not trust the map for them", text)

    def test_a_cache_without_its_sidecar_is_not_reused(self):
        first = self._pipe("cache-bare-one")
        self._write_census(first)
        for stray in (self.repo / ".team" / "census").glob("*.json"):
            stray.unlink()
        second = self._pipe("cache-bare-two")
        self.assertFalse(
            (second.work / CENSUS_ARTIFACT).is_file(),
            "staleness that cannot be computed is not staleness that is absent",
        )

    def test_the_cache_is_not_product(self):
        """.team/census is orchestrator scratch, like .team/work: a hop that
        writes there has not written the product tree."""
        first = self._pipe("cache-fence")
        self._write_census(first)
        cached = list((self.repo / ".team" / "census").glob("*.md"))
        self.assertTrue(cached)
        self.assertEqual(
            product_paths([str(p.relative_to(self.repo)) for p in cached]), []
        )
