"""Test-local interpreter of Claude CLI tool scope.

Not production. The pair that must agree is ``claude_cmd`` argv ↔ Claude's
``--allowedTools`` / ``--disallowedTools`` / ``--permission-mode`` language.
Grok ``--tools`` / ``--allow`` / ``--deny`` is not this language.

The reading here is the permissive one: under ``--permission-mode
acceptEdits`` an allow entry *pre-approves*, it does not fence. Only an
explicit ``--disallowedTools`` entry proves a tool is refused. Reading
``--allowedTools`` as an exhaustive allowlist would make every guard below
pass without the deny flags being emitted at all -- the vacuous-guard shape.
"""

from __future__ import annotations

from typing import List, Sequence

from tests.support.grok_argv import path_glob_matches


class ClaudeArgvNotClaudeLanguage(ValueError):
    """argv still speaks Grok tool/path spelling."""


def _flag_values(argv: Sequence[object], flag: str) -> List[str]:
    out: List[str] = []
    items = [str(x) for x in argv]
    for i, item in enumerate(items):
        if item == flag and i + 1 < len(items):
            out.append(items[i + 1])
    return out


def assert_claude_language(argv: Sequence[object]) -> None:
    items = [str(x) for x in argv]
    for flag in ("--tools", "--disallowed-tools", "--allow", "--deny"):
        if flag in items:
            raise ClaudeArgvNotClaudeLanguage(
                "Grok %s is not Claude tool-filter language: %s" % (flag, " ".join(items))
            )


def _specs(argv: Sequence[object], flag: str) -> List[str]:
    assert_claude_language(argv)
    out: List[str] = []
    for raw in _flag_values(argv, flag):
        for part in raw.split(","):
            spec = part.strip()
            if spec:
                out.append(spec)
    return out


def claude_flag_occurrences(argv: Sequence[object], flag: str) -> int:
    return len(_flag_values(argv, flag))


def _tool_and_glob(spec: str):
    if spec.endswith(")") and "(" in spec:
        name, glob = spec.split("(", 1)
        return name.strip(), glob[:-1].strip()
    return spec.strip(), ""


_WRITERS = ("Edit", "Write")


def claude_tool_permitted(argv: Sequence[object], tool: str) -> bool:
    """Whether this argv leaves an unscoped tool (Read, Bash, …) available."""
    deny = [_tool_and_glob(s) for s in _specs(argv, "--disallowedTools")]
    return not any(name == tool and not glob for name, glob in deny)


def claude_write_denied(argv: Sequence[object], rel: str) -> bool:
    """Whether a deny rule refuses Edit/Write on the repo-relative path.

    This is the load-bearing half of the Claude filter set, and the half
    that must match Grok's ``--deny``.
    """
    deny = [_tool_and_glob(s) for s in _specs(argv, "--disallowedTools")]
    for name, glob in deny:
        if name not in _WRITERS:
            continue
        if not glob or path_glob_matches(glob, rel):
            return True
    return False


def claude_write_permitted(argv: Sequence[object], rel: str) -> bool:
    """Whether this argv lets Claude Edit/Write the path.

    Not denied, and edits are auto-accepted. An ``Edit(root/**)`` allow entry
    does not narrow anything under acceptEdits -- see the module docstring;
    the git fence, not argv, is the boundary on the Claude side.
    """
    if claude_write_denied(argv, rel):
        return False
    return "acceptEdits" in [str(x) for x in argv]


def claude_allowed_write_roots(argv: Sequence[object]) -> List[str]:
    """Roots pre-approved for Edit/Write, in argv order."""
    out: List[str] = []
    for spec in _specs(argv, "--allowedTools"):
        name, glob = _tool_and_glob(spec)
        if name in _WRITERS and glob and glob not in out:
            out.append(glob)
    return out


def claude_read_tools_enabled(argv: Sequence[object]) -> bool:
    return all(
        claude_tool_permitted(argv, tool) for tool in ("Read", "Grep", "Glob", "LS")
    )


def claude_terminal_permitted(argv: Sequence[object]) -> bool:
    return claude_tool_permitted(argv, "Bash")


def claude_session_resumed(argv: Sequence[object]) -> bool:
    """True when this argv continues a thread rather than opening one.

    Claude spells it --resume <id>; the cold form is --session-id <id>.
    """
    return "--resume" in [str(a) for a in argv]


def claude_session_id(argv: Sequence[object]) -> str:
    for flag in ("--resume", "--session-id"):
        found = _flag_values(argv, flag)
        if found:
            return found[0]
    return ""
