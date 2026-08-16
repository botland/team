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
from team.pipeline import CENSUS_ARTIFACT, PipelineError, start_feature, start_range_review
from team.schemas import validate as validate_schema
from team.util import load_json
from tests.support.hostile import emit, register_runtime
from tests.support.hostile import HostileRuntime
from tests.support.repo import init_repo


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
        self.assertIn(str(pipe.work / CENSUS_ARTIFACT), later)

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
