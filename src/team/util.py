from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional


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


def under_root(path: str, root: str) -> bool:
    if not root:
        return True
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    root = root.replace("\\", "/").rstrip("/")
    return path == root or path.startswith(root + "/")


def posix(path: str) -> str:
    return str(path).replace("\\", "/")


def extract_json(payload: str) -> Any:
    """Best-effort parse of Claude/Grok headless JSON wrappers."""
    text = (payload or "").strip()
    if not text:
        return {}
    obj: Any
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = _last_object(text)
        if obj is None:
            return {"_raw": text}

    if not isinstance(obj, dict):
        return obj

    for key in ("structured_output", "structuredOutput"):
        if isinstance(obj.get(key), dict):
            return obj[key]

    result = obj.get("result")
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        inner = _try_json(result)
        if isinstance(inner, dict):
            return inner

    text_field = obj.get("text")
    if isinstance(text_field, str):
        inner = _try_json(text_field)
        if inner is not None:
            return inner

    # Already looks like a schema object.
    return obj


def _try_json(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _last_object(text)


def _last_object(text: str) -> Optional[Any]:
    decoder = json.JSONDecoder()
    last = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            val, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        last = val
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
