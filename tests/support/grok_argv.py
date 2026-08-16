"""Test-local interpreter of Grok CLI write scope.

Not production. The pair that must agree is ``grok_cmd`` argv ↔ Grok's
``--tools`` / ``--disallowed-tools`` / ``--allow`` / ``--deny`` language.
Claude ``Edit(`` / ``Write(`` / ``--allowedTools`` is not this language.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence


class GrokArgvNotGrokLanguage(ValueError):
    """argv still speaks Claude tool-filter spelling."""


def _flag_values(argv: Sequence[object], flag: str) -> List[str]:
    out: List[str] = []
    items = [str(x) for x in argv]
    i = 0
    while i < len(items):
        if items[i] == flag and i + 1 < len(items):
            out.append(items[i + 1])
            i += 2
            continue
        i += 1
    return out


def grok_flag_values(argv: Sequence[object], flag: str) -> List[str]:
    return _flag_values(argv, flag)


def _split_tool_list(values: Iterable[str]) -> List[str]:
    names: List[str] = []
    for raw in values:
        for part in str(raw).split(","):
            name = part.strip()
            if name:
                names.append(name)
    return names


def _looks_like_claude_tool_filter(value: str) -> bool:
    text = str(value)
    if "Edit(" in text or "Write(" in text or "NotebookEdit(" in text:
        return True
    if text.startswith("Edit") or text.startswith("Write"):
        return True
    return False


def assert_grok_write_language(argv: Sequence[object]) -> None:
    """Reject Claude filter spelling so a substring census cannot pass."""
    items = [str(x) for x in argv]
    joined = " ".join(items)
    if "--allowedTools" in items or "--disallowedTools" in items:
        raise GrokArgvNotGrokLanguage(
            "Claude --allowedTools / --disallowedTools is not Grok language: %s" % joined
        )
    for token in items:
        if _looks_like_claude_tool_filter(token):
            raise GrokArgvNotGrokLanguage(
                "Claude Edit/Write tool filters are not Grok --allow/--deny path globs: %s"
                % token
            )


def _normalize_glob(raw: str) -> str:
    text = str(raw).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if text.endswith("/**"):
        text = text[:-3]
    return text.rstrip("/")


def path_glob_matches(glob: str, rel: str) -> bool:
    """Grok path glob: ``root`` or ``root/**`` matches that root and descendants."""
    root = _normalize_glob(glob)
    path = str(rel).replace("\\", "/").lstrip("./")
    if not root or root == ".":
        return True
    return path == root or path.startswith(root + "/")


_GROK_READ_TOOLS = ("read_file", "grep", "list_dir")


def grok_read_tools_enabled(argv: Sequence[object]) -> bool:
    """Whether this argv enables Grok's inspect/read tool set.

    ``--tools`` is an allowlist. An empty list means the CLI default
    (reads enabled). Any listed set must keep every read tool; a
    disallowed read tool fails closed.
    """
    assert_grok_write_language(argv)
    tools = set(_split_tool_list(_flag_values(argv, "--tools")))
    disallowed = set(_split_tool_list(_flag_values(argv, "--disallowed-tools")))
    if any(name in disallowed for name in _GROK_READ_TOOLS):
        return False
    if not tools:
        return True
    return all(name in tools for name in _GROK_READ_TOOLS)


def grok_search_replace_permitted(argv: Sequence[object], rel: str) -> bool:
    """Whether this argv lets Grok ``search_replace`` the repo-relative path.

    Raises ``GrokArgvNotGrokLanguage`` if argv is still Claude spelling.
    """
    assert_grok_write_language(argv)
    tools = set(_split_tool_list(_flag_values(argv, "--tools")))
    disallowed = set(_split_tool_list(_flag_values(argv, "--disallowed-tools")))
    allow = _flag_values(argv, "--allow")
    deny = _flag_values(argv, "--deny")
    if "search_replace" in disallowed:
        return False
    if tools and "search_replace" not in tools:
        return False
    for glob in deny:
        if path_glob_matches(glob, rel):
            return False
    if allow:
        return any(path_glob_matches(glob, rel) for glob in allow)
    # No allow list: write-code ``code_root='.'`` (deny roots only) or unscoped.
    return True
