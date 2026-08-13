from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from team.util import as_str, explicit_roots, extract_json, write_text


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


def headless_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Force non-interactive CLIs. Claude and Grok both grow a TUI unless told not to."""
    env = dict(os.environ if base is None else base)
    env["CI"] = "1"
    env["TEAM_HEADLESS"] = "1"
    # Claude Code treats this as "no interactive chrome".
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    return env


def describe_runtime_failure(result: Result) -> str:
    """Short human line. Do not dump the Claude/Grok wrapper JSON."""
    blob: Any = result.output if isinstance(result.output, dict) else {}
    text = "\n".join(p for p in (result.error, result.raw) if p) or "unknown"
    if not blob:
        raw = text.strip()
        brace = raw.find("{")
        if brace >= 0:
            try:
                parsed = json.loads(raw[brace:])
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                blob = parsed
    if isinstance(blob, dict):
        status = blob.get("api_error_status")
        msg = as_str(blob.get("result") or blob.get("error") or "")
        if status == 429 or "session limit" in msg.lower():
            return msg or "provider session limit (429)"
        if blob.get("is_error") and msg:
            return msg
        if status and msg:
            return "provider error %s: %s" % (status, msg)
    line = " ".join(str(text).split())
    if len(line) > 240:
        line = line[:237] + "..."
    return line or "unknown"


def resolve_session(stored: str) -> Tuple[str, bool]:
    """Return (session_id, resume).

    Both CLIs reject ``--session-id`` for an ID that already exists.
    If this role already has a thread, resume it. Never create with a used ID.
    """
    sid = stored or ""
    if sid:
        return sid, True
    return "", False


def claude_cmd(
    *,
    prompt: str,
    schema: Optional[Dict[str, Any]],
    capability: str,
    session_id: str,
    resume: bool,
    extra: Optional[Dict[str, Any]] = None,
) -> list:
    extra = extra or {}
    bin_ = extra.get("claude_bin") or os.environ.get("TEAM_CLAUDE", "claude")
    # -p/--print is what turns Claude's TUI off. Never invoke without it.
    cmd = [bin_, "-p", prompt, "--output-format", "json"]
    if resume and session_id:
        cmd.extend(["--resume", session_id])
    else:
        cmd.extend(["--session-id", session_id])
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
        extra = extra or {}
        code_roots = explicit_roots(extra.get("code_root"))
        test_roots = explicit_roots(extra.get("test_root"))
        if capability == "write-tests":
            for root in test_roots:
                cmd.extend(["--allowedTools", "Edit(%s/**)" % root])
            for root in code_roots:
                if root not in test_roots:
                    cmd.extend(["--disallowedTools", "Edit(%s/**)" % root])
        else:
            for root in code_roots:
                cmd.extend(["--allowedTools", "Edit(%s/**)" % root])
            for root in test_roots:
                if root not in code_roots:
                    cmd.extend(["--disallowedTools", "Edit(%s/**)" % root])
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
    return cmd


def grok_cmd(
    *,
    prompt_path: Path,
    schema: Optional[Dict[str, Any]],
    capability: str,
    session_id: str,
    resume: bool,
    repo: Path,
    extra: Optional[Dict[str, Any]] = None,
) -> list:
    extra = extra or {}
    bin_ = extra.get("grok_bin") or os.environ.get("TEAM_GROK", "grok")
    # --prompt-file (or -p) is headless. --no-alt-screen stops the fullscreen TUI
    # even if a user config prefers it. Do not pass --fullscreen or --minimal.
    cmd = [
        bin_,
        "--no-alt-screen",
        "--cwd",
        str(repo),
        "--output-format",
        "json",
        "--prompt-file",
        str(prompt_path),
    ]
    if resume and session_id:
        cmd.extend(["-r", session_id])
    else:
        cmd.extend(["--session-id", session_id])
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
        code_roots = explicit_roots(extra.get("code_root"))
        test_roots = explicit_roots(extra.get("test_root"))
        if capability == "write-tests":
            for root in test_roots:
                cmd.extend(["--allow", "Edit(%s/**)" % root])
                cmd.extend(["--allow", "Write(%s/**)" % root])
            for root in code_roots:
                if root not in test_roots:
                    cmd.extend(["--deny", "Edit(%s/**)" % root])
                    cmd.extend(["--deny", "Write(%s/**)" % root])
        if capability == "write-code":
            for root in code_roots:
                cmd.extend(["--allow", "Edit(%s/**)" % root])
                cmd.extend(["--allow", "Write(%s/**)" % root])
            for root in test_roots:
                if root not in code_roots:
                    cmd.extend(["--deny", "Edit(%s/**)" % root])
                    cmd.extend(["--deny", "Write(%s/**)" % root])
    return cmd


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
        cmd = claude_cmd(
            prompt=prompt,
            schema=schema,
            capability=capability,
            session_id=sid,
            resume=resume and bool(session_id),
            extra=extra,
        )
        write_text(work / "prompts" / ("%s.cmd.txt" % phase), " ".join(cmd[:8]) + " …")
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
        cmd = grok_cmd(
            prompt_path=prompt_path,
            schema=schema,
            capability=capability,
            session_id=sid,
            resume=resume and bool(session_id),
            repo=repo,
            extra=extra,
        )
        write_text(work / "prompts" / ("%s.cmd.txt" % phase), " ".join(cmd))
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


_REGISTRY = {
    "claude": ClaudeRuntime,
    "grok": GrokRuntime,
    "fake": FakeRuntime,
}


def register(name: str, factory: Any) -> None:
    _REGISTRY[name] = factory


def runtime_for(name: str) -> Runtime:
    factory = _REGISTRY.get(name)
    if factory is None:
        raise RuntimeError_("Unknown runtime: %s" % name)
    if isinstance(factory, type):
        return factory()
    if callable(factory):
        built = factory()
        if isinstance(built, Runtime):
            return built
        return built
    return factory


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
            env=headless_env(),
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
    if raw.strip() and parsed is None:
        return Result(
            success=False,
            output={},
            session_id=session_id,
            raw=raw + "\n" + (proc.stderr or ""),
            error="parse failure: unparseable stdout",
        )
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {"value": parsed}
    if proc.returncode != 0:
        return Result(
            success=False,
            output=parsed if isinstance(parsed, dict) else {},
            session_id=session_id,
            raw=raw + "\n" + (proc.stderr or ""),
            error="exit %s: %s" % (proc.returncode, (proc.stderr or raw)[-2000:]),
        )
    if parsed.get("is_error") or parsed.get("api_error_status"):
        failed = Result(
            success=False,
            output=parsed,
            session_id=as_str(parsed.get("session_id") or parsed.get("sessionId") or session_id),
            raw=raw,
            error=as_str(parsed.get("result") or parsed.get("error") or "provider error"),
        )
        failed.error = describe_runtime_failure(failed)
        return failed
    sid = as_str(parsed.get("session_id") or parsed.get("sessionId") or session_id)
    output = parsed
    return Result(
        success=True,
        output=output,
        session_id=sid,
        raw=raw,
        error="",
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
            "attacks": [],
            "critic_markdown": "Design survived the attacks; brief is covered.",
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
                    "path": "%s/test_adversarial.py" % test_root,
                }
            ],
            "adversarial_markdown": "Added a test for an empty greet.",
            "paths_touched": ["%s/test_adversarial.py" % test_root],
        },
        "tester": {
            "passed": True,
            "report_markdown": "Host suite is authoritative; fake tester has nothing to add.",
            "command_used": "true",
        },
        "debugger": {
            "owner": "implementer",
            "root_cause": "fake diagnosis",
            "diagnosis_markdown": "Fake diagnosis: inspect greet implementation.",
        },
        "guardian": {
            "risks": [],
            "chain": {
                "r_to_a": {"ok": True, "note": "fake: brief matches design"},
                "a_to_t": {"ok": True, "note": "fake: contract covers criteria"},
                "t_to_i": {"ok": True, "note": "fake: impl satisfies contract"},
                "i_to_r": {"ok": True, "note": "fake: impl satisfies brief"},
            },
            "guardian_markdown": "Chain R→A→T→I→R holds in fake mode.",
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
    if phase.startswith("seq-reviewer"):
        return dict(canned["reviewer"])
    if phase.startswith("seq-guardian"):
        return dict(canned["guardian"])
    if phase.startswith("repair-test"):
        return dict(canned["test-writer"])
    if phase.startswith("repair-implementer"):
        return dict(canned["implementer"])
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
    if (
        phase == "test-writer"
        or phase.startswith("test-writer-")
        or phase.startswith("repair-test")
    ):
        path = repo / test_root / "test_greet.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                "def test_greet_returns_hello():\n    assert True\n",
                encoding="utf-8",
            )
    if (
        phase == "implementer"
        or phase.startswith("implementer-")
        or phase.startswith("repair-implementer")
    ):
        path = repo / code_root / "greet.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    if phase == "adversarial":
        path = repo / test_root / "test_adversarial.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                "def test_adversarial_empty_ok():\n    assert True\n",
                encoding="utf-8",
            )
