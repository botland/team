from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence


def engine_root() -> Path:
    import os

    env = os.environ.get("TEAM_HOME")
    if env:
        return Path(env).resolve()
    # src/team/util.py -> src/team -> src -> engine root
    return Path(__file__).resolve().parents[2]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")
    return path


def load_json(path: Path) -> Any:
    return json.loads(read_text(path))


def write_atomic(path: Path, text: str) -> Path:
    """Write text so a reader never sees a partial file.

    The mechanism dump_json documents, for callers whose payload is not JSON.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    # Unique per call, not per process: two threads writing the same path would
    # otherwise share one temp name and the second os.replace would find it
    # already renamed away.
    tmp = path.with_name("%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def dump_json(path: Path, obj: Any) -> Path:
    """Write JSON so a reader never sees a partial document.

    ``write_text`` truncates and then writes: a concurrent or interrupted write
    leaves a prefix on disk, and a *shorter* write landing inside a longer one
    leaves a complete document followed by trailing garbage -- which parses as a
    JSONDecodeError at load and takes down every command that touches the slug.
    Rename is atomic within a filesystem, so readers see the old file or the new
    one and never a half of either. The temp file is a sibling to keep the
    rename on one filesystem.
    """
    return write_atomic(path, json.dumps(obj, indent=2, sort_keys=True))


def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if not text:
        text = "feature"
    return text[:max_len].rstrip("-")


def normalize_root(value: Any) -> str:
    """One encoding for write roots: posix, no ``./`` prefix, no trailing slash.

    ``'.'`` stays ``'.'``. Whitespace-only is unset (``''``).
    """
    text = posix(str(value or "")).strip()
    if not text:
        return ""
    while text.startswith("./"):
        text = text[2:]
    if text in ("", "."):
        return "."
    return text.rstrip("/") or "."


def explicit_roots(value: Any) -> list:
    """Non-empty write roots. Whitespace-only and None are unset."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    out = []
    for item in items:
        if item is None:
            continue
        text = normalize_root(item)
        if text:
            out.append(text)
    return out


def escapes_repo(path: str) -> bool:
    """True when a path is absolute or walks above the repository root."""
    text = posix(path or "").strip()
    if not text:
        return False
    if text.startswith("/"):
        return True
    depth = 0
    for part in text.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if depth == 0:
                return True
            depth -= 1
        else:
            depth += 1
    return False


def under_root(path: str, root: str) -> bool:
    if not root:
        return True
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    root = root.replace("\\", "/").rstrip("/")
    if root in (".",):
        return True
    return path == root or path.startswith(root + "/")


def _foreign_submodule_roots(
    role_root: Any,
    submodule_paths: Optional[Sequence[Any]] = None,
) -> List[str]:
    """Submodules this write root does not own.

    ``role_root='.'`` is the repository, so every submodule is foreign.
    A submodule is in-scope only when the role root is that path or inside it.
    """
    root = normalize_root(role_root)
    out: List[str] = []
    seen = set()
    for raw in submodule_paths or ():
        sub = normalize_root(raw)
        if not sub or sub in seen:
            continue
        if root and root != "." and (root == sub or under_root(root, sub)):
            continue
        seen.add(sub)
        out.append(sub)
    return out


def denied_write_code_roots(
    code_root: Any,
    test_root: Any,
    submodule_paths: Optional[Sequence[Any]] = None,
) -> List[str]:
    """write-code exclusions: testhost's test tree and foreign git submodules.

    ``code_root='.'`` is the repository, so every submodule is foreign.
    A submodule is in-scope only when ``code_root`` is that path or inside it.
    The deny list uses testhost's discovery fallback so an unset, empty, or
    whitespace ``test_root`` still names the conventional ``tests`` tree.
    That resolved root is excluded unless it is ``'.'`` or the same as
    ``code_root``.
    """
    from team.testhost import _python_fallback_root

    code = normalize_root(code_root)
    test = _python_fallback_root(test_root)
    out: List[str] = []
    if test and test != "." and test != code:
        out.append(test)
    for sub in _foreign_submodule_roots(code, submodule_paths):
        if sub not in out:
            out.append(sub)
    return out


def denied_write_test_roots(
    code_root: Any,
    test_root: Any,
    submodule_paths: Optional[Sequence[Any]] = None,
) -> List[str]:
    """write-tests exclusions: code_root and foreign git submodules.

    ``test_root='.'`` is the repository, so production must still be denied
    when ``code_root`` is a distinct tree. A submodule is in-scope only when
    ``test_root`` is that path or inside it. ``code_root`` is excluded unless
    it is unset, ``'.'``, or the same as ``test_root``.
    """
    code = normalize_root(code_root)
    test = normalize_root(test_root)
    out: List[str] = []
    if code and code != "." and code != test:
        out.append(code)
    for sub in _foreign_submodule_roots(test, submodule_paths):
        if sub not in out:
            out.append(sub)
    return out


def posix(path: str) -> str:
    return str(path).replace("\\", "/")


_TRAILING_MODEL_TOKENS = ("<|eos|>", "<|endoftext|>")


def extract_json(payload: str, schema: Optional[Dict[str, Any]] = None) -> Any:
    """Best-effort parse of Claude/Grok headless JSON wrappers.

    Grok ``--output-format json`` wraps the model text in ``{text, sessionId}``
    and, with ``--json-schema``, may also set ``structuredOutput``. Multi-turn
    ``text`` concatenates intermediate objects; the model may suffix ``<|eos|>``.
    Take the last *top-level* value, not the last nested ``{``. When ``schema``
    is given, required keys identify a finished artifact. Empty schema
    arrays are a legitimate clean result (no findings, accepting critic).
    Prefer the first finished candidate in field order
    (``structuredOutput`` / ``result`` / ``text`` / wrapper). An incomplete
    or empty-looking fragment must not beat a finished sibling. In
    concatenated ``text``, the last finished object wins — including a
    finished empty-array object over a stale non-empty sibling.
    """
    text = (payload or "").strip()
    if not text:
        return {}
    obj: Any
    try:
        obj = json.loads(_strip_trailing_tokens(text))
    except json.JSONDecodeError:
        obj = _last_object(text, schema)
        if obj is None:
            return None

    if not isinstance(obj, dict):
        return obj

    candidates: List[Any] = []

    def add(val: Any) -> None:
        if val is None:
            return
        if schema is None or _has_required(val, schema):
            candidates.append(val)

    for key in ("structured_output", "structuredOutput"):
        if key not in obj:
            continue
        add(_coerce_json(obj.get(key), schema))

    result = obj.get("result")
    if isinstance(result, dict):
        add(result)
    elif isinstance(result, str):
        add(_try_json(result, schema))

    text_field = obj.get("text")
    if isinstance(text_field, str):
        add(_try_json(text_field, schema))

    add(obj)

    chosen = _prefer_schema_match(candidates, schema)
    if chosen is not None:
        return chosen
    if isinstance(text_field, str):
        fallback = _try_json(text_field, schema)
        if fallback is not None:
            return fallback
    return obj


def _strip_trailing_tokens(text: str) -> str:
    text = (text or "").strip()
    changed = True
    while changed:
        changed = False
        for token in _TRAILING_MODEL_TOKENS:
            if text.endswith(token):
                text = text[: -len(token)].rstrip()
                changed = True
    return text


def _coerce_json(value: Any, schema: Optional[Dict[str, Any]] = None) -> Any:
    """Grok sometimes emits structuredOutput as a JSON string."""
    if isinstance(value, str):
        parsed = _try_json(value, schema)
        return parsed if parsed is not None else value
    return value


def _has_required(obj: Any, schema: Dict[str, Any]) -> bool:
    if not isinstance(schema, dict):
        return True
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(obj, dict):
            return False
        return all(key in obj for key in schema.get("required") or [])
    if expected == "array":
        return isinstance(obj, list)
    return True


def _schema_array_keys(schema: Dict[str, Any]) -> List[str]:
    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        return []
    keys: List[str] = []
    for key, spec in props.items():
        if isinstance(spec, dict) and spec.get("type") == "array":
            keys.append(key)
    return keys


def _schema_vacuous(obj: Any, schema: Dict[str, Any]) -> bool:
    """True when obj is not a finished required-keys artifact.

    Empty schema arrays are not vacuous when every required key is
    present — that is a legitimate clean review / accepting critic.
    Missing required keys (or a non-object) with empty-looking arrays
    is still vacuous so a fragment cannot beat a finished sibling.
    No array properties means nothing is vacuous.
    """
    if not isinstance(obj, dict) or not isinstance(schema, dict):
        return False
    if _has_required(obj, schema):
        return False
    arrays = _schema_array_keys(schema)
    if not arrays:
        return False
    return all(not as_list(obj.get(key)) for key in arrays)


def _prefer_schema_match(
    candidates: Sequence[Any], schema: Optional[Dict[str, Any]]
) -> Any:
    """First finished (required-keys) match, including empty schema arrays.

    Wrapper fields and concatenated text share this rule. Callers pass
    candidates in field order (structured output, result, text, raw).
    An incomplete fragment does not beat a finished sibling.
    """
    rows = [c for c in candidates if c is not None]
    if not rows:
        return None
    if schema is None:
        return rows[0]
    finished = [c for c in rows if _has_required(c, schema)]
    if finished:
        return finished[0]
    return rows[0]


def _try_json(text: str, schema: Optional[Dict[str, Any]] = None) -> Any:
    text = _strip_trailing_tokens(text)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _last_object(text, schema)


def _iter_json_values(text: str) -> Iterator[Any]:
    decoder = json.JSONDecoder()
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] not in "{[":
            index += 1
        if index >= length:
            return
        try:
            val, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        yield val
        index = end


def _last_object(text: str, schema: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """Last top-level JSON value. Nested braces are not candidates.

    Optional ``schema``: last finished required-keys object (empty arrays
    allowed). A trailing incomplete object does not wipe a prior finished
    one.
    """
    last = None
    matches: List[Any] = []
    for val in _iter_json_values(text):
        last = val
        if schema is None or _has_required(val, schema):
            matches.append(val)
    if not matches:
        return last
    if schema is None:
        return matches[-1]
    chosen = _prefer_schema_match(list(reversed(matches)), schema)
    return chosen if chosen is not None else last


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return default


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return default


def as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    return []


def first_existing(paths: Iterable[Path]) -> Optional[Path]:
    for path in paths:
        if path.is_file():
            return path
    return None
