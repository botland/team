from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional


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


def dump_json(path: Path, obj: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if not text:
        text = "feature"
    return text[:max_len].rstrip("-")


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
        text = str(item).strip()
        if text:
            out.append(text)
    return out


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


def posix(path: str) -> str:
    return str(path).replace("\\", "/")


_TRAILING_MODEL_TOKENS = ("<|eos|>", "<|endoftext|>")


def extract_json(payload: str, schema: Optional[Dict[str, Any]] = None) -> Any:
    """Best-effort parse of Claude/Grok headless JSON wrappers.

    Grok ``--output-format json`` wraps the model text in ``{text, sessionId}``
    and, with ``--json-schema``, may also set ``structuredOutput``. Multi-turn
    ``text`` concatenates intermediate objects; the model may suffix ``<|eos|>``.
    Take the last *top-level* value, not the last nested ``{``. When ``schema``
    is given, prefer an object that has that schema's required keys.
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

    def take(val: Any) -> Any:
        if val is None:
            return None
        if schema is None or _has_required(val, schema):
            return val
        return None

    for key in ("structured_output", "structuredOutput"):
        if key not in obj:
            continue
        inner = take(_coerce_json(obj.get(key), schema))
        if inner is not None:
            return inner

    result = obj.get("result")
    if isinstance(result, dict):
        inner = take(result)
        if inner is not None:
            return inner
    if isinstance(result, str):
        inner = take(_try_json(result, schema))
        if inner is not None:
            return inner

    text_field = obj.get("text")
    if isinstance(text_field, str):
        inner = take(_try_json(text_field, schema))
        if inner is not None:
            return inner

    matched = take(obj)
    if matched is not None:
        return matched
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

    Optional ``schema``: last value that carries the schema's required keys.
    """
    last = None
    last_match = None
    for val in _iter_json_values(text):
        last = val
        if schema is None or _has_required(val, schema):
            last_match = val
    if schema is not None and last_match is not None:
        return last_match
    return last


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
