"""Hostile runtime double: writes, deletes, commits, or crashes on demand.

Reachable only from the test tree. Not a shipped runtime name.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from team.runners import Result


def write(path: str, text: str, *, append: bool = False) -> tuple:
    return ("write", path, text, append)


def delete(path: str) -> tuple:
    return ("delete", path)


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
        actions: Sequence[tuple],
        *,
        phases: Optional[Iterable[str]] = None,
        num_turns: Optional[int] = None,
    ) -> None:
        self.actions = list(actions)
        self.phases = set(phases) if phases is not None else None
        self.num_turns = num_turns
        self.calls: List[Dict[str, str]] = []

    def _match(self, phase: str) -> bool:
        if self.phases is None:
            return True
        return phase in self.phases

    def complete(self, **kwargs: Any) -> Result:
        phase = kwargs.get("phase") or ""
        role = kwargs.get("role") or ""
        capability = kwargs.get("capability") or ""
        repo = Path(kwargs["repo"])
        session_id = kwargs.get("session_id") or "hostile"
        self.calls.append({"role": role, "phase": phase, "capability": capability})
        if not self._match(phase):
            from team.runners import _fake_output

            extra = kwargs.get("extra") or {}
            return Result(
                success=True,
                output=_fake_output(phase, extra),
                session_id=session_id,
                raw="",
                num_turns=self.num_turns,
            )
        output: Dict[str, Any] = {
            "summary": "hostile",
            "paths_touched": [],
            "ready": True,
            "consult": "none",
            "questions": [],
            "findings": [],
        }
        for action in self.actions:
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
                )
        return Result(
            success=True,
            output=output,
            session_id=session_id,
            raw="",
            num_turns=self.num_turns,
        )


@contextmanager
def register_runtime(name: str, runtime: Any):
    import team.pipeline as pipeline
    import team.runners as runners

    orig = runners.runtime_for

    def wrapped(n: str):
        if n == name:
            return runtime
        return orig(n)

    runners.runtime_for = wrapped
    pipeline.runtime_for = wrapped
    try:
        yield
    finally:
        runners.runtime_for = orig
        pipeline.runtime_for = orig
