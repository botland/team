from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from team.util import as_str, extract_json, write_text


@dataclass
class Result:
    success: bool
    output: Dict[str, Any]
    session_id: str
    raw: str
    error: str = ""


class RuntimeError_(RuntimeError):
    pass


class Runtime:
    name = "base"

    def complete(
        self,
        *,
        role: str,
        phase: str,
        prompt: str,
        schema: Optional[Dict[str, Any]],
        capability: str,
        session_id: str = "",
        resume: bool = False,
        work: Path,
        repo: Path,
        timeout: int = 1800,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Result:
        raise NotImplementedError


class ClaudeRuntime(Runtime):
    name = "claude"

    def complete(
        self,
        *,
        role: str,
        phase: str,
        prompt: str,
        schema: Optional[Dict[str, Any]],
        capability: str,
        session_id: str = "",
        resume: bool = False,
        work: Path,
        repo: Path,
        timeout: int = 1800,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Result:
        extra = extra or {}
        sid = session_id or str(uuid.uuid4())
        prompt_path = write_text(work / "prompts" / ("%s.prompt.md" % phase), prompt)
        cmd = [
            extra.get("claude_bin") or os.environ.get("TEAM_CLAUDE", "claude"),
            "-p",
            prompt,
            "--output-format",
            "json",
            "--session-id",
            sid,
        ]
        if resume and session_id:
            cmd = [
                extra.get("claude_bin") or os.environ.get("TEAM_CLAUDE", "claude"),
                "-p",
                prompt,
                "--output-format",
                "json",
                "--resume",
                session_id,
            ]
            sid = session_id
        if schema:
            cmd.extend(["--json-schema", json.dumps(schema)])
        if extra.get("effort"):
            cmd.extend(["--effort", str(extra["effort"])])
        if capability == "read-only":
            cmd.extend(
                [
                    "--permission-mode",
                    "acceptEdits",
                    "--allowedTools",
                    "Read,Grep,Glob,LS",
                    "--disallowedTools",
                    "Edit,Write,NotebookEdit",
                ]
            )
        elif capability in ("write-tests", "write-code"):
            cmd.extend(["--permission-mode", "acceptEdits"])
        elif capability == "execute":
            cmd.extend(
                [
                    "--permission-mode",
                    "acceptEdits",
                    "--allowedTools",
                    "Read,Grep,Glob,LS,Bash",
                    "--disallowedTools",
                    "Edit,Write,NotebookEdit",
                ]
            )
        return _run(cmd, repo=repo, timeout=timeout, session_id=sid, prompt_path=prompt_path)


class GrokRuntime(Runtime):
    name = "grok"

    def complete(
        self,
        *,
        role: str,
        phase: str,
        prompt: str,
        schema: Optional[Dict[str, Any]],
        capability: str,
        session_id: str = "",
        resume: bool = False,
        work: Path,
        repo: Path,
        timeout: int = 1800,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Result:
        extra = extra or {}
        sid = session_id or str(uuid.uuid4())
        prompt_path = write_text(work / "prompts" / ("%s.prompt.md" % phase), prompt)
        bin_ = extra.get("grok_bin") or os.environ.get("TEAM_GROK", "grok")
        cmd = [bin_, "--cwd", str(repo), "--session-id", sid, "--output-format", "json"]
        if resume and session_id:
            cmd = [bin_, "--cwd", str(repo), "-r", session_id, "--output-format", "json"]
            sid = session_id
        if extra.get("no_memory", True):
            cmd.append("--no-memory")
        if schema:
            cmd.extend(["--json-schema", json.dumps(schema)])
        if capability == "read-only":
            cmd.extend(
                [
                    "--tools",
                    "read_file,grep,list_dir",
                    "--disallowed-tools",
                    "search_replace",
                ]
            )
        elif capability == "execute":
            cmd.extend(
                [
                    "--tools",
                    "read_file,grep,list_dir,run_terminal_cmd",
                    "--disallowed-tools",
                    "search_replace",
                    "--always-approve",
                ]
            )
        elif capability in ("write-tests", "write-code"):
            cmd.append("--always-approve")
            code_root = extra.get("code_root") or ""
            test_root = extra.get("test_root") or ""
            if capability == "write-tests":
                if test_root:
                    cmd.extend(["--allow", "Edit(%s/**)" % test_root])
                    cmd.extend(["--allow", "Write(%s/**)" % test_root])
                if code_root and code_root != test_root:
                    cmd.extend(["--deny", "Edit(%s/**)" % code_root])
                    cmd.extend(["--deny", "Write(%s/**)" % code_root])
            if capability == "write-code":
                if code_root:
                    cmd.extend(["--allow", "Edit(%s/**)" % code_root])
                    cmd.extend(["--allow", "Write(%s/**)" % code_root])
                if test_root and test_root != code_root:
                    cmd.extend(["--deny", "Edit(%s/**)" % test_root])
                    cmd.extend(["--deny", "Write(%s/**)" % test_root])
        cmd.extend(["--prompt-file", str(prompt_path)])
        return _run(cmd, repo=repo, timeout=timeout, session_id=sid, prompt_path=prompt_path)


class FakeRuntime(Runtime):
    name = "fake"

    def complete(
        self,
        *,
        role: str,
        phase: str,
        prompt: str,
        schema: Optional[Dict[str, Any]],
        capability: str,
        session_id: str = "",
        resume: bool = False,
        work: Path,
        repo: Path,
        timeout: int = 1800,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Result:
        extra = extra or {}
        sid = session_id or "fake-%s" % phase
        write_text(work / "prompts" / ("%s.prompt.md" % phase), prompt)
        output = _fake_output(phase, extra)
        _maybe_write_fake_files(phase, repo, extra)
        return Result(success=True, output=output, session_id=sid, raw=json.dumps(output))


def runtime_for(name: str) -> Runtime:
    if name in ("claude",):
        return ClaudeRuntime()
    if name in ("grok",):
        return GrokRuntime()
    if name in ("fake",):
        return FakeRuntime()
    raise RuntimeError_("Unknown runtime: %s" % name)


def _run(
    cmd: list,
    *,
    repo: Path,
    timeout: int,
    session_id: str,
    prompt_path: Path,
) -> Result:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout if timeout > 0 else None,
        )
    except FileNotFoundError as exc:
        return Result(
            success=False,
            output={},
            session_id=session_id,
            raw="",
            error="executable not found: %s" % exc,
        )
    except subprocess.TimeoutExpired:
        return Result(
            success=False,
            output={},
            session_id=session_id,
            raw="",
            error="timeout after %ss" % timeout,
        )
    raw = proc.stdout or ""
    parsed = extract_json(raw) if raw.strip() else {}
    if not isinstance(parsed, dict):
        parsed = {"value": parsed}
    if proc.returncode != 0 and not parsed:
        return Result(
            success=False,
            output={},
            session_id=session_id,
            raw=raw + "\n" + (proc.stderr or ""),
            error="exit %s: %s" % (proc.returncode, (proc.stderr or raw)[-2000:]),
        )
    sid = as_str(parsed.get("session_id") or parsed.get("sessionId") or session_id)
    # Strip wrapper keys if the model also echoed them.
    output = parsed
    return Result(
        success=True,
        output=output,
        session_id=sid,
        raw=raw,
        error=(proc.stderr or "") if proc.returncode != 0 else "",
    )


def _fake_output(phase: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    code_root = extra.get("code_root") or "src"
    test_root = extra.get("test_root") or "tests"
    canned = {
        "architect": {
            "design_markdown": (
                "# Design\n\n## Goals\nAdd a greet helper.\n\n"
                "## Non-goals\nNetwork I/O.\n\n"
                "## Acceptance criteria\n- greet returns a hello string\n\n"
                "## Invariants\n- no network\n"
            ),
            "code_root": code_root,
            "test_root": test_root,
            "acceptance_criteria": ["greet returns a hello string"],
            "structural_touchpoints": ["%s/greet" % code_root],
            "invariants": ["no network"],
        },
        "critic": {
            "accepts": True,
            "issues": [],
            "critic_markdown": "Design matches the brief.",
        },
        "tdd-design": {
            "ready": True,
            "questions": [],
            "test_contract_markdown": (
                "# Test contract\n\n"
                "Criterion: greet returns a hello string\n"
                "- test: test_greet_returns_hello\n"
                "- assert: result contains hello\n"
            ),
            "criteria_map": [
                {
                    "criterion": "greet returns a hello string",
                    "tests": ["test_greet_returns_hello"],
                }
            ],
        },
        "test-writer-gate": {
            "ready": True,
            "consult": "none",
            "questions": [],
            "summary": "ready",
        },
        "test-writer": {
            "summary": "added test_greet_returns_hello",
            "paths_touched": ["%s/test_greet.py" % test_root],
        },
        "implementer-gate": {
            "ready": True,
            "consult": "none",
            "questions": [],
            "summary": "ready",
        },
        "implementer": {
            "summary": "added greet helper",
            "paths_touched": ["%s/greet.py" % code_root],
        },
        "reviewer": {
            "summary": "No blocking issues in fake mode.",
            "findings": [],
            "review_markdown": "Fake review: artifacts are consistent by construction.",
        },
        "adversarial": {
            "vectors": [
                {
                    "title": "empty name",
                    "threat": "greet may return empty",
                    "covered_by_existing_test": False,
                }
            ],
            "adversarial_markdown": "Try greet with an empty name.",
        },
        "debugger": {
            "owner": "implementer",
            "root_cause": "fake diagnosis",
            "diagnosis_markdown": "Fake diagnosis: inspect greet implementation.",
        },
        "guardian": {
            "risks": [],
            "guardian_markdown": "No invariant risks detected in fake mode.",
        },
        "replan-questions": {
            "questions_for_tdd": [],
            "questions_for_implementer": [],
            "notes": "fake replan",
        },
        "replan": {
            "design_markdown": (
                "# Design replan\n\n"
                "## Unchanged assumptions\n- greet helper\n\n"
                "## Changed assumptions\n- none\n\n"
                "## New acceptance criteria\n- none\n\n"
                "## Removed acceptance criteria\n- none\n\n"
                "## Structural changes\n- none\n"
            ),
            "code_root": code_root,
            "test_root": test_root,
            "acceptance_criteria": ["greet returns a hello string"],
            "structural_touchpoints": ["%s/greet" % code_root],
            "invariants": ["no network"],
        },
        "answers": {"answers_markdown": "Fake answers: proceed."},
        "scout": {
            "roots": ["."],
            "components": [
                {
                    "name": "readme",
                    "path": "README",
                    "state": "done",
                    "evidence": "README exists",
                }
            ],
            "notes": "fake inventory",
        },
        "assess": {
            "status_markdown": (
                "# Status\n\n"
                "## Finished\n- README (`README`) — present\n\n"
                "## WIP\n- none observed in fake mode\n\n"
                "## Missing\n- none observed in fake mode\n"
            ),
            "summary": "Tiny repo with a README.",
        },
    }
    if phase in canned:
        return dict(canned[phase])
    if phase.startswith("consult"):
        return dict(canned["answers"])
    for key in sorted(canned, key=len, reverse=True):
        if phase.startswith(key + "-") or phase.startswith(key + ":"):
            return dict(canned[key])
    # default gate-like
    return {"ready": True, "consult": "none", "questions": [], "summary": "fake"}


def _maybe_write_fake_files(phase: str, repo: Path, extra: Dict[str, Any]) -> None:
    code_root = extra.get("code_root") or "src"
    test_root = extra.get("test_root") or "tests"
    if phase == "test-writer":
        path = repo / test_root / "test_greet.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                "def test_greet_returns_hello():\n    assert True\n",
                encoding="utf-8",
            )
    if phase == "implementer":
        path = repo / code_root / "greet.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
