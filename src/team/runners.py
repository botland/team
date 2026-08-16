from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from team.config import EFFORT_ALIASES, may_write
from team.usage import Usage, parse_usage
from team.util import (
    as_str,
    denied_write_code_roots,
    denied_write_test_roots,
    explicit_roots,
    extract_json,
    write_text,
)


@dataclass
class Result:
    success: bool
    output: Dict[str, Any]
    session_id: str
    raw: str
    error: str = ""
    num_turns: Optional[int] = None
    usage: Optional[Usage] = None


class RuntimeError_(RuntimeError):
    pass


# Each runtime's own ladder, placed on the neutral 0..5 scale. This is the seam
# where the vendor vocabulary lives; nothing upstream of argv knows these words.
#
# Verified against the CLIs themselves, not from memory:
#   claude --effort bogus -> "Valid values: low, medium, high, xhigh, max."
#   grok --effort bogus   -> "use one of: xhigh, high, medium, low"
#
# Claude has five rungs, Grok four. Grok has nothing at 5, so a run asking for
# the maximum gets Grok's xhigh rather than silently losing the setting. Neither
# has a rung at 0; both floor to "low", their lowest.
CLAUDE_EFFORT_LADDER = {"low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}
GROK_EFFORT_LADDER = {"low": 1, "medium": 2, "high": 3, "xhigh": 4}


def resolve_effort(level: Any, ladder: Dict[str, int]) -> str:
    """Neutral 0..5 -> the nearest rung this runtime actually implements.

    Claude *warns and ignores* an unknown --effort value and Grok exits non-zero,
    so passing a level through unmapped either silently drops the setting or
    kills the hop. Snapping is the only behaviour that is correct on both.

    Ties break downward: when a request sits between two rungs, the cheaper one
    is the safer reading of "closest".

    Legacy names are accepted here as well as in the config layer. Callers reach
    argv from more than one path, and the cost of a stray "xhigh" arriving as a
    string is a silently unset effort -- the exact failure this function exists
    to prevent. One alias table, imported from config.
    """
    if isinstance(level, str) and level.strip().lower() in EFFORT_ALIASES:
        want = EFFORT_ALIASES[level.strip().lower()]
    else:
        try:
            want = int(level)
        except (TypeError, ValueError):
            return ""
    return min(ladder, key=lambda name: (abs(ladder[name] - want), ladder[name]))


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


def _failure_blob(result: Result) -> Tuple[Dict[str, Any], str]:
    """(wrapper dict, raw text) for a failed result. Parses an embedded JSON tail."""
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
    return (blob if isinstance(blob, dict) else {}), text


def is_quota_failure(result: Result) -> bool:
    """True when the provider refused on quota, not on anything the hop did.

    A hop that ran out of session budget is not a defective hop: its prompt,
    schema, and tree are all still valid, and the same call succeeds after the
    window resets. Callers must not treat it as a hop defect.
    """
    blob, _text = _failure_blob(result)
    msg = as_str(blob.get("result") or blob.get("error") or "").lower()
    return (
        blob.get("api_error_status") == 429
        or "session limit" in msg
        or "rate limit" in msg
        or "usage limit" in msg
    )


def describe_runtime_failure(result: Result) -> str:
    """Short human line. Do not dump the Claude/Grok wrapper JSON."""
    blob, text = _failure_blob(result)
    if blob:
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
    """CLI helper: a stored vendor id cannot be created again.

    Not hop-identity authority. ``Pipeline.invoke`` always mints a new
    ``session_id`` and passes ``resume=False``. This only documents the
    vendor constraint that ``--session-id`` rejects an already-used id.
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
    effort = resolve_effort(extra.get("effort"), CLAUDE_EFFORT_LADDER)
    if effort:
        cmd.extend(["--effort", effort])
    cmd.extend(["--permission-mode", "acceptEdits"])
    if may_write(capability):
        allow, deny = write_tool_path_filters(capability, extra)
        # Same shape as Grok's write hop: read tools kept, the writer scoped
        # to the role roots, the terminal off. write-* is not execute.
        cmd.extend(
            [
                "--allowedTools",
                _claude_tool_list(list(_CLAUDE_READ_TOOLS) + _write_tool_globs(allow)),
                "--disallowedTools",
                _claude_tool_list(_CLAUDE_UNSCOPED_WRITE + _write_tool_globs(deny)),
            ]
        )
        return cmd
    # Fail closed: unknown capabilities are inspect, not writers.
    allowed = list(_CLAUDE_READ_TOOLS)
    denied = ["Edit", "Write", "NotebookEdit"]
    if capability == "execute":
        allowed.append("Bash")
    else:
        # Grok's --tools allowlist withholds run_terminal_cmd from an inspect
        # hop; an unlisted tool on this side is not refused, so say it.
        denied.append("Bash")
    cmd.extend(
        [
            "--allowedTools",
            _claude_tool_list(allowed),
            "--disallowedTools",
            _claude_tool_list(denied),
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
    effort = resolve_effort(extra.get("effort"), GROK_EFFORT_LADDER)
    if effort:
        cmd.extend(["--effort", effort])
    # --tools is an allowlist. search_replace requires Read
    # (skip_read_before_edit=false); a write-only list fails session init.
    if may_write(capability):
        cmd.append("--always-approve")
        cmd.extend(["--tools", _grok_tools("search_replace")])
        allow, deny = write_tool_path_filters(capability, extra)
        for glob in _grok_path_globs(allow):
            cmd.extend(["--allow", glob])
        for glob in _grok_path_globs(deny):
            cmd.extend(["--deny", glob])
        return cmd
    extra_tools = ("run_terminal_cmd",) if capability == "execute" else ()
    cmd.extend(
        [
            "--tools",
            _grok_tools(*extra_tools),
            "--disallowed-tools",
            "search_replace",
        ]
    )
    if capability == "execute":
        cmd.append("--always-approve")
    return cmd


_GROK_READ_TOOLS = ("read_file", "grep", "list_dir")


def _grok_tools(*extra: str) -> str:
    """Grok --tools allowlist. Read tools are always first."""
    names = list(_GROK_READ_TOOLS)
    for name in extra:
        if name and name not in names:
            names.append(name)
    return ",".join(names)


def write_tool_path_filters(
    capability: str, extra: Optional[Dict[str, Any]]
) -> Tuple[List[str], List[str]]:
    """Relative roots to allow and deny for Edit/Write tool filters.

    ``code_root='.'`` is not turned into ``Edit(./**)`` — that glob misses
    repo-root files. Accept-edits / always-approve plus denied roots is the
    repo-level write-code scope.
    """
    extra = extra or {}
    code_roots = explicit_roots(extra.get("code_root"))
    test_roots = explicit_roots(extra.get("test_root"))
    raw_subs = extra.get("submodule_paths") or []
    if isinstance(raw_subs, str):
        subs = [raw_subs]
    else:
        subs = [str(s) for s in raw_subs]
    allow: List[str] = []
    deny: List[str] = []
    if capability == "write-tests":
        allow.extend(test_roots)
        deny.extend(
            denied_write_test_roots(
                extra.get("code_root") or "",
                extra.get("test_root") or "",
                subs,
            )
        )
    elif capability == "write-code":
        for root in code_roots:
            if root != ".":
                allow.append(root)
        deny.extend(
            denied_write_code_roots(
                extra.get("code_root") or "",
                extra.get("test_root") or "",
                subs,
            )
        )
    return allow, deny


_CLAUDE_READ_TOOLS = ("Read", "Grep", "Glob", "LS")

# Writers with no path filter, and the terminal. A write hop is not an execute
# hop: Grok's --tools allowlist already withholds run_terminal_cmd, and an
# unscoped writer would defeat the path globs beside it.
_CLAUDE_UNSCOPED_WRITE = ["Bash", "NotebookEdit"]


def _claude_tool_list(specs: List[str]) -> str:
    """One comma-joined value per flag. Claude's tool filter syntax is
    comma-separated, so a comma inside a root cannot be expressed in it --
    fail loudly rather than emit a filter that silently means something else.
    Repeating the flag instead would leave union-vs-last-wins undecided.
    """
    for spec in specs:
        if "," in spec:
            raise RuntimeError_(
                "path is not expressible as a Claude tool filter (comma): %s" % spec
            )
    return ",".join(specs)


_WRITE_TOOLS = ("Edit", "Write")


def _write_tool_globs(roots: List[str]) -> List[str]:
    """Edit and Write globs for the same allow/deny roots. Not a second list."""
    from team.util import normalize_root

    out: List[str] = []
    for raw in roots:
        root = normalize_root(raw)
        if not root or root == ".":
            continue
        for tool in _WRITE_TOOLS:
            out.append("%s(%s/**)" % (tool, root))
    return out


def _grok_path_globs(roots: List[str]) -> List[str]:
    """Grok --allow/--deny path globs. Not Claude Edit/Write tool filters."""
    from team.util import normalize_root

    out: List[str] = []
    for raw in roots:
        root = normalize_root(raw)
        if not root or root == ".":
            continue
        out.append("%s/**" % root)
    return out


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
        return _run(
            cmd,
            repo=repo,
            timeout=timeout,
            session_id=sid,
            prompt_path=prompt_path,
            schema=schema,
        )


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
        return _run(
            cmd,
            repo=repo,
            timeout=timeout,
            session_id=sid,
            prompt_path=prompt_path,
            schema=schema,
        )


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
        if capability in ("write-code", "write-tests"):
            _maybe_write_fake_files(phase, repo, extra)
        # Canned inspect already “ran”; missing turns would fail-closed.
        return Result(
            success=True,
            output=output,
            session_id=sid,
            raw=json.dumps(output),
            num_turns=2,
        )


_REGISTRY = {
    "claude": ClaudeRuntime,
    "grok": GrokRuntime,
    "fake": FakeRuntime,
}

_SHIPPED = frozenset(_REGISTRY)


def register(name: str, factory: Any) -> None:
    _REGISTRY[name] = factory


def unregister(name: str) -> None:
    if name in _SHIPPED:
        raise ValueError("cannot unregister shipped runtime %s" % name)
    _REGISTRY.pop(name, None)


def is_registered(name: str) -> bool:
    return name in _REGISTRY


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
    schema: Optional[Dict[str, Any]] = None,
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
    wrapper = _envelope(raw)
    usage = parse_usage(wrapper)
    parsed = extract_json(raw, schema=schema) if raw.strip() else {}
    if raw.strip() and parsed is None:
        return Result(
            success=False,
            output={},
            session_id=session_id,
            raw=raw + "\n" + (proc.stderr or ""),
            error="parse failure: unparseable stdout",
            usage=usage,
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
            usage=usage,
        )
    if parsed.get("is_error") or parsed.get("api_error_status"):
        failed = Result(
            success=False,
            output=parsed,
            session_id=as_str(parsed.get("session_id") or parsed.get("sessionId") or session_id),
            raw=raw,
            error=as_str(parsed.get("result") or parsed.get("error") or "provider error"),
            usage=usage,
        )
        failed.error = describe_runtime_failure(failed)
        return failed
    sid = as_str(
        (wrapper or {}).get("session_id")
        or (wrapper or {}).get("sessionId")
        or parsed.get("session_id")
        or parsed.get("sessionId")
        or session_id
    )
    output = parsed
    return Result(
        success=True,
        output=output,
        session_id=sid,
        raw=raw,
        error="",
        num_turns=_num_turns(wrapper),
        usage=usage,
    )


def _envelope(raw: str) -> Optional[Dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _num_turns(wrapper: Optional[Dict[str, Any]]) -> Optional[int]:
    if not wrapper or wrapper.get("num_turns") is None:
        return None
    try:
        return int(wrapper["num_turns"])
    except (TypeError, ValueError):
        return None


# Inspect-before-JSON: keyed by role, not runtime. Role→runtime is data.
# Labeled approximation — not the read-only write fence.
_INSPECT_ROLES = frozenset(
    ("reviewer", "guardian", "scout", "critic", "debugger")
)


def inspect_turn_count(payload: Any) -> Optional[int]:
    """Turn count persisted on an inspect-role result artifact.

    Reads the same fields ``Pipeline.invoke`` writes (top-level ``num_turns``
    and ``_meta.num_turns``). Missing or null is unfinished.
    """
    if not isinstance(payload, dict):
        return None
    sources = [payload]
    meta = payload.get("_meta")
    if isinstance(meta, dict):
        sources.append(meta)
    for source in sources:
        if "num_turns" not in source:
            continue
        raw = source.get("num_turns")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return None


_WRITE_CAPS = frozenset(("write-code", "write-tests"))


def unfinished_write(
    *,
    capability: str,
    num_turns: Optional[int],
    product_delta: Any,
    output: Any,
) -> bool:
    """True when a write hop did not produce a product-tree delta.

    A 1-turn schema dump with no file changes is not implementation.
    Paths under ``.team/work`` are not a product delta.
    """
    if capability not in _WRITE_CAPS:
        return False
    if inspect_progress_note(output):
        return True
    premature = num_turns is None or num_turns <= 1
    if not premature:
        return False
    return not list(product_delta or [])


def unfinished_inspect(*, role: str, num_turns: Optional[int], output: Any) -> bool:
    """Shared unfinished predicate for invoke and collect.

    Progress notes are unfinished even with more turns. Inspect roles
    fail closed on missing or ``<=1`` turns. Not derived from capability.
    """
    if inspect_progress_note(output):
        return True
    if role not in _INSPECT_ROLES:
        return False
    if num_turns is None or num_turns <= 1:
        return True
    return False


_PROGRESS_MARKDOWN = frozenset(
    (
        "drafting",
        "still reading",
        "in progress",
        "review in progress",
        "wip",
        "working",
    )
)
_PROMISED_FINDINGS = (
    "issues are below",
    "findings below",
    "highest-severity",
)


def inspect_progress_note(output: Any) -> bool:
    """Schema-legal progress object — not a finished inspect artifact."""
    if not isinstance(output, dict):
        return False
    summary = as_str(output.get("summary")).strip().lower()
    if "reviewing the collected range first" in summary:
        return True
    markdown = (
        as_str(output.get("review_markdown")).strip().lower()
        or as_str(output.get("guardian_markdown")).strip().lower()
        or as_str(output.get("diagnosis_markdown")).strip().lower()
    )
    if markdown in _PROGRESS_MARKDOWN:
        return True
    if markdown.startswith("starting read-only"):
        return True
    findings = [item for item in (output.get("findings") or []) if isinstance(item, dict)]
    if not findings and any(token in summary for token in _PROMISED_FINDINGS):
        return True
    for item in findings:
        if as_str(item.get("title")).strip().lower() == "review in progress":
            return True
    return False


_FAKE_CENSUS = (
    "# Census\n\n"
    "## Layout\n- src/\n- tests/\n\n"
    "## Missing layers\n- none in fake mode\n\n"
    "## Verified facts\n- greet helper is the fake product surface\n"
)


def _with_census(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(payload)
    out.setdefault("census_markdown", _FAKE_CENSUS)
    return out


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
            "disposition": "retry",
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
        return _fake_payload(phase, dict(canned[phase]))
    if phase.startswith("seq-reviewer"):
        return _fake_payload(phase, dict(canned["reviewer"]))
    if phase.startswith("seq-guardian"):
        return _fake_payload(phase, dict(canned["guardian"]))
    if phase.startswith("repair-test"):
        return dict(canned["test-writer"])
    if phase.startswith("repair-implementer"):
        return dict(canned["implementer"])
    if phase.startswith("consult"):
        return _fake_payload(phase, dict(canned["answers"]))
    for key in sorted(canned, key=len, reverse=True):
        if phase.startswith(key + "-") or phase.startswith(key + ":"):
            return _fake_payload(phase, dict(canned[key]))
    # default gate-like
    return {"ready": True, "consult": "none", "questions": [], "summary": "fake"}


_FAKE_CENSUS_PHASES = frozenset(
    (
        "architect",
        "critic",
        "tdd-design",
        "reviewer",
        "guardian",
        "replan",
        "replan-questions",
        "answers",
        "scout",
        "assess",
        "debugger",
    )
)


def _fake_payload(phase: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Inspect hops may seed census.md. Writers do not."""
    key = phase.split(":")[0]
    if key in _FAKE_CENSUS_PHASES or any(
        phase.startswith(name + "-") for name in _FAKE_CENSUS_PHASES
    ):
        return _with_census(payload)
    return payload


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
