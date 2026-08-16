from __future__ import annotations

from typing import Any, Dict, List


def validate(obj: Any, schema: Dict[str, Any], *, enums: bool = True) -> List[str]:
    """Stdlib JSON Schema subset: type, required, properties, items, maxItems, enum."""
    errors: List[str] = []
    _check(obj, schema or {}, "$", errors, enums=enums)
    return errors


def _check(
    obj: Any, schema: Dict[str, Any], path: str, errors: List[str], *, enums: bool
) -> None:
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    if isinstance(expected, list):
        if obj is None:
            if "null" in expected:
                return
            errors.append("%s: expected %s" % (path, " | ".join(str(t) for t in expected)))
            return
        for t in expected:
            if t == "null":
                continue
            nested: List[str] = []
            sub = dict(schema)
            sub["type"] = t
            _check(obj, sub, path, nested, enums=enums)
            if not nested:
                return
        errors.append("%s: expected %s" % (path, " | ".join(str(t) for t in expected)))
        return
    if expected == "object":
        if not isinstance(obj, dict):
            errors.append("%s: expected object" % path)
            return
        for key in schema.get("required") or []:
            if key not in obj:
                errors.append("%s: missing %s" % (path, key))
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in obj and isinstance(sub, dict):
                child = key if path == "$" else "%s.%s" % (path, key)
                _check(obj[key], sub, child, errors, enums=enums)
        return
    if expected == "array":
        if not isinstance(obj, list):
            errors.append("%s: expected array" % path)
            return
        max_items = schema.get("maxItems")
        if max_items is not None and len(obj) > int(max_items):
            errors.append("%s: maxItems %s" % (path, max_items))
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(obj):
                _check(item, item_schema, "%s[%d]" % (path, i), errors, enums=enums)
        return
    if expected == "string":
        if not isinstance(obj, str):
            errors.append("%s: expected string" % path)
            return
    elif expected == "boolean":
        if not isinstance(obj, bool):
            errors.append("%s: expected boolean" % path)
            return
    elif expected == "integer":
        if type(obj) is not int:
            errors.append("%s: expected integer" % path)
            return
    elif expected == "number":
        if not isinstance(obj, (int, float)) or isinstance(obj, bool):
            errors.append("%s: expected number" % path)
            return
    enum = schema.get("enum")
    if enums and enum is not None and obj not in enum:
        errors.append("%s: %r not in enum" % (path, obj))
