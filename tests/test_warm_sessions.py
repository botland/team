"""Warm chains: an accelerator, never the channel.

[run] warm lets consecutive hops of one role+runtime+capability resume a
session instead of paying to re-derive the same tree knowledge. The property
that keeps it honest is that dropping any link and running that hop cold
produces the same artifacts -- files are still the protocol either way.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from team.config import load_config, parse_bool
from team.pipeline import WARM_CONTEXT_CEILING, start_feature
from team.runners import Usage
from tests.support.hostile import HostileRuntime, emit, register_runtime
from tests.support.repo import init_repo


DESIGN = {
    "design_markdown": "# D\n",
    "code_root": "src",
    "test_root": "tests",
    "census_markdown": "# Census\n\nlayout\n",
}


class WarmChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ["TEAM_HOME"] = str(ROOT)
        os.environ.pop("TEAM_WARM", None)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("TEAM_WARM", None)

    def _pipe(self, slug, *, warm):
        cfg = load_config(
            self.repo,
            fake=True,
            force=True,
            code_root="src",
            test_root="tests",
            warm=warm,
        )
        return start_feature(cfg, "brief", slug)

    def _two_hops(self, pipe, usage=None, turns=2):
        seen = []

        class Recorder(HostileRuntime):
            def complete(self, **kw):
                seen.append((kw.get("session_id"), kw.get("resume")))
                return super().complete(**kw)

        rt = Recorder(
            [emit(DESIGN), emit(DESIGN)],
            phases=("architect", "architect-revise"),
            num_turns=turns,
            usage=usage,
        )
        with register_runtime("fake", rt):
            pipe.invoke("architect", "architect", "p", "design.json")
            pipe.invoke("architect", "architect-revise", "p", "design.json")
        return seen

    def test_cold_is_the_default_and_mints_a_new_session_each_hop(self):
        seen = self._two_hops(self._pipe("cold", warm=False))
        self.assertEqual([resume for _sid, resume in seen], [False, False])
        self.assertNotEqual(seen[0][0], seen[1][0])

    def test_warm_continues_the_same_role_runtime_capability(self):
        pipe = self._pipe("warm", warm=True)
        seen = self._two_hops(pipe, usage=Usage(input_tokens=10, output_tokens=1))
        self.assertFalse(seen[0][1], "the first hop of a chain has nothing to resume")
        self.assertTrue(seen[1][1], "the second continues it")
        self.assertEqual(seen[1][0], seen[0][0])

    def test_a_different_capability_does_not_inherit_the_chain(self):
        """The gate/write pair stays cold: a resumed hop re-declaring
        different tool filters is vendor semantics nothing here executes."""
        pipe = self._pipe("cap", warm=True)
        seen = []

        class Recorder(HostileRuntime):
            def complete(self, **kw):
                seen.append((kw.get("capability"), kw.get("resume")))
                return super().complete(**kw)

        with register_runtime(
            "fake",
            Recorder(
                [emit(DESIGN)],
                phases=("architect",),
                num_turns=2,
                usage=Usage(input_tokens=10),
            ),
        ):
            pipe.invoke("architect", "architect", "p", "design.json")
        with register_runtime(
            "fake",
            Recorder(
                [emit({"summary": "s", "paths_touched": ["tests/a.py"]})],
                phases=("writer",),
                num_turns=2,
                usage=Usage(input_tokens=10),
            ),
        ):
            pipe.invoke(
                "test-writer",
                "writer",
                "p",
                "write_summary.json",
                capability="write-tests",
            )
        self.assertEqual([resume for _cap, resume in seen], [False, False])

    def test_a_chain_breaks_once_the_context_is_bigger_than_a_cold_hop(self):
        pipe = self._pipe("ceiling", warm=True)
        fat = Usage(
            input_tokens=0,
            cache_read_input_tokens=(WARM_CONTEXT_CEILING + 1) * 2,
        )
        seen = self._two_hops(pipe, usage=fat, turns=2)
        self.assertFalse(
            seen[1][1],
            "a session past the ceiling costs more per turn than a cold hop",
        )
        self.assertIn("warm chain", "\n".join(pipe.log_lines))

    def test_a_failed_hop_does_not_hand_its_session_on(self):
        pipe = self._pipe("failed", warm=True)
        rt = HostileRuntime(
            [emit({"nope": 1}), emit(DESIGN)],
            phases=("architect", "architect-revise"),
            num_turns=2,
            usage=Usage(input_tokens=1),
        )
        seen = []
        with register_runtime("fake", rt):
            try:
                pipe.invoke("architect", "architect", "p", "design.json")
            except Exception:
                pass
        with register_runtime(
            "fake",
            HostileRuntime(
                [emit(DESIGN)],
                phases=("architect-revise",),
                num_turns=2,
                usage=Usage(input_tokens=1),
            ),
        ):
            pipe.invoke("architect", "architect-revise", "p", "design.json")
        chains = pipe._warm_chains
        self.assertTrue(
            all(v.get("session") for v in chains.values()),
            "a chain only ever holds a session from a hop that succeeded",
        )

    def test_warm_and_cold_produce_the_same_artifacts(self):
        """The property that makes a chain droppable. If this ever fails,
        the session has become the channel and files no longer are.

        Same slug, two repos: comparing two slugs in one repo would only
        prove that slug names differ.
        """
        usage = Usage(input_tokens=10, output_tokens=1)

        def run(warm):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            repo = Path(tmp.name)
            init_repo(repo)
            cfg = load_config(
                repo,
                fake=True,
                force=True,
                code_root="src",
                test_root="tests",
                warm=warm,
            )
            pipe = start_feature(cfg, "brief", "equiv")
            self._two_hops(pipe, usage=usage)
            return {
                str(p.relative_to(pipe.work)): p.read_text(encoding="utf-8")
                # The protocol is the artifacts. prompts/ and the spend ledger
                # record hop mechanics -- session ids among them -- and are
                # expected to differ between a warm run and a cold one.
                for p in sorted(pipe.work.rglob("*"))
                if p.is_file()
                and p.suffix in (".md", ".txt")
                and "prompts" not in p.parts
                and not p.name.startswith("usage")
            }

        self.assertEqual(run(False), run(True))


class WarmSpellingTests(unittest.TestCase):
    """--warm, TEAM_WARM and [run] warm resolve through one function."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        init_repo(self.repo)
        os.environ.pop("TEAM_WARM", None)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("TEAM_WARM", None)

    def test_every_accepted_spelling_means_the_same_thing(self):
        for text in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(parse_bool(text, what="x"))
        for text in ("0", "false", "no", "off"):
            self.assertFalse(parse_bool(text, what="x"))

    def test_a_word_that_is_not_a_boolean_is_an_error_not_a_false(self):
        with self.assertRaises(SystemExit):
            parse_bool("maybe", what="run.warm")

    def test_the_env_var_turns_it_on(self):
        os.environ["TEAM_WARM"] = "true"
        self.assertTrue(load_config(self.repo).warm)

    def test_off_by_default(self):
        self.assertFalse(load_config(self.repo).warm)


if __name__ == "__main__":
    unittest.main()
