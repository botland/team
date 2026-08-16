"""reviewer="both" runs two invokes against one Pipeline. State must survive it.

`--fake` collapses every role to a single runtime, so the dual-review path has no
end-to-end coverage through the ordinary fake rail. These tests reach it by
registering two slow doubles under the real runtime names, which is the only way
to make the concurrency observable at all.

Fail direction: both failures here are silent in production. A dropped recording
means `collect_review_findings` skips one reviewer's results while `review.md`
still shows both, so `apply` routes half the findings and nothing says so. A torn
`state.json` means every later command on the slug dies in `State.load`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.config import load_config
from team.pipeline import start_feature
from team.runners import Result, _fake_output
from team.state import State
from team.util import dump_json
from tests.support.hostile import register_runtimes
from tests.support.repo import init_repo


class SlowRuntime:
    """Holds the hop open long enough for the two threads to actually overlap."""

    def __init__(self, name: str, delay: float = 0.05) -> None:
        self.name = name
        self.delay = delay

    def complete(self, **kwargs):
        time.sleep(self.delay)
        return Result(
            success=True,
            output=_fake_output(kwargs.get("phase") or "", kwargs.get("extra") or {}),
            session_id=str(uuid.uuid4()),
            raw="",
            num_turns=2,
        )


class DualReviewRaceTests(unittest.TestCase):
    RUNS = 8

    def setUp(self):
        os.environ["TEAM_HOME"] = str(ROOT)

    def _run_one(self, slug: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        init_repo(repo)
        cfg = load_config(repo, force=True, code_root=".", test_root="tests")
        cfg.roles["reviewer"] = "both"
        pipe = start_feature(cfg, "brief", slug)
        doubles = {"claude": SlowRuntime("claude"), "grok": SlowRuntime("grok")}
        with register_runtimes(doubles):
            pipe.phase_reviewer()
        return pipe

    def test_both_reviewers_are_recorded_every_run(self):
        """The converse of "both ran": both must also be *pinned*.

        An unrecorded result file is dropped by collect_review_findings as a
        stale extra, so losing the pin loses the findings without an error.
        """
        for i in range(self.RUNS):
            pipe = self._run_one("race-rec-%d" % i)
            rec = pipe.state.last_review or {}
            names = sorted(str(r.get("name")) for r in rec.get("results") or [])
            self.assertEqual(
                names,
                ["reviewer-claude.result.json", "reviewer-grok.result.json"],
                "run %d recorded %s" % (i, names),
            )

    def test_state_json_stays_loadable_every_run(self):
        for i in range(self.RUNS):
            pipe = self._run_one("race-state-%d" % i)
            try:
                State.load(pipe.work)
            except json.JSONDecodeError as exc:
                self.fail("run %d wrote an unparseable state.json: %s" % (i, exc))

    def test_both_reviews_reach_the_merged_artifact(self):
        pipe = self._run_one("race-merge")
        for runtime in ("claude", "grok"):
            self.assertTrue(
                (pipe.work / ("review-%s.md" % runtime)).is_file(),
                "review-%s.md missing" % runtime,
            )
        self.assertTrue((pipe.work / "review.md").is_file())


class AtomicDumpTests(unittest.TestCase):
    """dump_json is the shared writer; atomicity belongs there, not per caller."""

    def test_concurrent_writers_never_leave_a_partial_document(self):
        """Mutation check for this guard: swapping dump_json for a truncate+write
        makes this fail, which is what makes the assertion worth having."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "state.json"
        big = {"k": ["x" * 200 for _ in range(400)]}
        small = {"k": ["y"]}
        stop = threading.Event()
        errors = []

        def writer(payload):
            while not stop.is_set():
                dump_json(path, payload)

        def reader():
            while not stop.is_set():
                try:
                    text = path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                if not text:
                    continue
                try:
                    json.loads(text)
                except json.JSONDecodeError as exc:
                    errors.append(str(exc))
                    return

        threads = [
            threading.Thread(target=writer, args=(big,)),
            threading.Thread(target=writer, args=(small,)),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        time.sleep(1.0)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(errors, [], "reader saw a partial document: %s" % errors[:1])

    def test_replaces_in_place_without_leaving_temp_files(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        dump_json(d / "a.json", {"v": 1})
        dump_json(d / "a.json", {"v": 2})
        self.assertEqual(json.loads((d / "a.json").read_text()), {"v": 2})
        self.assertEqual([p.name for p in d.iterdir()], ["a.json"])


if __name__ == "__main__":
    unittest.main()
