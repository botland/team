"""Hostile runtime double: writes, deletes, commits, or crashes on demand.

Reachable only from the test tree. Not a shipped runtime name.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from team.runners import Result
from team.usage import Usage


def write(path: str, text: str, *, append: bool = False) -> tuple:
    return ("write", path, text, append)


def delete(path: str) -> tuple:
    return ("delete", path)


def rename(src: str, dest: str) -> tuple:
    """Real ``git mv`` so porcelain emits `` -> `` (not unlink+write)."""
    return ("rename", src, dest)


def commit(msg: str, paths: Optional[Sequence[str]] = None) -> tuple:
    return ("commit", msg, list(paths) if paths else None)


def emit(data: Dict[str, Any]) -> tuple:
    return ("emit", data)


def crash(exit_code: int, stdout: str = "") -> tuple:
    return ("crash", exit_code, stdout)


class HostileRuntime:
    name = "hostile"

    def __init__(
        self,
        actions: Optional[Sequence[tuple]] = None,
        *,
        phases: Optional[Iterable[str]] = None,
        num_turns: Optional[int] = 2,
        by_phase: Optional[Dict[str, Sequence[tuple]]] = None,
        usage: Optional[Usage] = None,
        census: bool = True,
    ) -> None:
        # Emitted inspect payloads get the same census seeding FakeRuntime
        # applies, so this double does not silently differ from what it stands
        # in for. Pass census=False when the *absence* is the thing under test.
        self.census = census
        self.actions = list(actions or [])
        self.phases = set(phases) if phases is not None else None
        # Finished-inspect default (same as FakeRuntime). Pass num_turns=None
        # only when the test is about missing turns.
        self.num_turns = num_turns
        self.by_phase = {str(k): list(v) for k, v in (by_phase or {}).items()}
        self.usage = usage
        self.calls: List[Dict[str, str]] = []

    def _match(self, phase: str) -> bool:
        if self.by_phase:
            return phase in self.by_phase
        if self.phases is None:
            return True
        return phase in self.phases

    def _run_actions(
        self,
        actions: Sequence[tuple],
        *,
        repo: Path,
        session_id: str,
        phase: str = "",
    ) -> Result:
        output: Dict[str, Any] = {
            "summary": "hostile",
            "paths_touched": [],
            "ready": True,
            "consult": "none",
            "questions": [],
            "findings": [],
        }
        for action in actions:
            kind = action[0]
            if kind == "write":
                _, rel, text, append = action
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                if append and path.is_file():
                    path.write_text(
                        path.read_text(encoding="utf-8") + text, encoding="utf-8"
                    )
                else:
                    path.write_text(text, encoding="utf-8")
            elif kind == "delete":
                path = repo / action[1]
                if path.exists() or path.is_symlink():
                    path.unlink()
            elif kind == "rename":
                _, src, dest = action
                dest_path = repo / dest
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                subprocess.check_call(
                    ["git", "-C", str(repo), "mv", "--", src, dest]
                )
            elif kind == "commit":
                _, msg, paths = action
                if paths:
                    subprocess.check_call(
                        ["git", "-C", str(repo), "add", "--", *list(paths)]
                    )
                else:
                    subprocess.check_call(["git", "-C", str(repo), "add", "-A"])
                subprocess.check_call(
                    ["git", "-C", str(repo), "commit", "-m", msg],
                    stdout=subprocess.DEVNULL,
                )
            elif kind == "emit":
                output = dict(action[1])
            elif kind == "crash":
                _, code, stdout = action
                return Result(
                    success=False,
                    output={},
                    session_id=session_id,
                    raw=stdout,
                    error="exit %s" % code,
                    num_turns=self.num_turns,
                    usage=self.usage,
                )
        # The pair that must agree: this double stands in for FakeRuntime, and
        # its *unmatched* branch below already returns _fake_output. Without the
        # same seeding here, a matched inspect emit is the only path in the
        # engine that produces a census-less inspect result, and every test
        # using it dies on the range reviewer's census guard rather than on the
        # thing it was written to check. setdefault, so an explicit
        # census_markdown in the emit still wins.
        from team.runners import _fake_payload

        if self.census:
            output = _fake_payload(phase, output)
        return Result(
            success=True,
            output=output,
            session_id=session_id,
            raw="",
            num_turns=self.num_turns,
            usage=self.usage,
        )

    def complete(self, **kwargs: Any) -> Result:
        phase = kwargs.get("phase") or ""
        role = kwargs.get("role") or ""
        capability = kwargs.get("capability") or ""
        repo = Path(kwargs["repo"])
        session_id = kwargs.get("session_id") or "hostile"
        self.calls.append(
            {
                "role": role,
                "phase": phase,
                "capability": capability,
                "session_id": session_id,
                "resume": kwargs.get("resume"),
            }
        )
        if phase in self.by_phase:
            return self._run_actions(
                self.by_phase[phase], repo=repo, session_id=session_id, phase=phase
            )
        if not self._match(phase):
            from team.runners import _fake_output

            extra = kwargs.get("extra") or {}
            return Result(
                success=True,
                output=_fake_output(phase, extra),
                session_id=session_id,
                raw="",
                num_turns=self.num_turns,
                usage=self.usage,
            )
        return self._run_actions(
            self.actions, repo=repo, session_id=session_id, phase=phase
        )


@contextmanager
def register_runtime(name: str, runtime: Any):
    names = (name,) if isinstance(name, str) else tuple(name)
    mapping = {n: runtime for n in names}
    with register_runtimes(mapping):
        yield


@contextmanager
def register_runtimes(mapping: Dict[str, Any]):
    import team.pipeline as pipeline
    import team.runners as runners

    orig = runners.runtime_for
    table = dict(mapping)

    def wrapped(n: str):
        if n in table:
            return table[n]
        return orig(n)

    runners.runtime_for = wrapped
    pipeline.runtime_for = wrapped
    try:
        yield
    finally:
        runners.runtime_for = orig
        pipeline.runtime_for = orig
