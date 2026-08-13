from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from team.util import engine_root, write_text

# role -> default runtime, allowed runtimes, capability
ROLES: Dict[str, Dict[str, Any]] = {
    "architect": {
        "default": "claude",
        "runtimes": ("claude", "grok"),
        "capability": "read-only",
        "optional": False,
    },
    "critic": {
        "default": "claude",
        "runtimes": ("claude", "grok"),
        "capability": "read-only",
        "optional": True,
    },
    "tdd-design": {
        "default": "claude",
        "runtimes": ("claude", "grok"),
        "capability": "read-only",
        "optional": False,
    },
    "test-writer": {
        "default": "grok",
        "runtimes": ("claude", "grok"),
        "capability": "write-tests",
        "optional": False,
    },
    "implementer": {
        "default": "grok",
        "runtimes": ("claude", "grok"),
        "capability": "write-code",
        "optional": False,
    },
    "tester": {
        "default": "host",
        "runtimes": ("host", "claude", "grok"),
        "capability": "execute",
        "optional": False,
    },
    "adversarial": {
        "default": "grok",
        "runtimes": ("claude", "grok"),
        "capability": "write-tests",
        "optional": True,
    },
    "debugger": {
        "default": "claude",
        "runtimes": ("claude", "grok"),
        "capability": "read-only",
        "optional": True,
    },
    "reviewer": {
        "default": "both",
        "runtimes": ("claude", "grok", "both"),
        "capability": "read-only",
        "optional": False,
    },
    "guardian": {
        "default": "claude",
        "runtimes": ("claude", "grok"),
        "capability": "read-only",
        "optional": True,
    },
    "scout": {
        "default": "grok",
        "runtimes": ("claude", "grok"),
        "capability": "read-only",
        "optional": False,
    },
}

PHASE_ORDER = [
    "architect",
    "critic",
    "tdd-design",
    "test-writer",
    "baseline",
    "implementer",
    "final-test",
    "debugger",
    "repair",
    "verify-test",
    "adversarial",
    "adversarial-test",
    "reviewer",
    "guardian",
]

PHASE_ALIASES = {
    "tdd": "tdd-design",
    "design": "tdd-design",
    "tests": "test-writer",
    "impl": "implementer",
    "implement": "implementer",
    "test": "final-test",
    "review": "reviewer",
    "replan": "replan",
    "audit": "scout",
    "assess": "assess",
    "repair": "repair",
    "verify": "verify-test",
    "attack": "adversarial",
}

AUDIT_PHASE_ORDER = [
    "scout",
    "assess",
    "reviewer",
]

RANGE_PHASE_ORDER = [
    "reviewer",
    "guardian",
]

DEFAULT_AUDIT_QUERY = "Status of this workspace: WIP, finished, missing"

DEPTH_ALIASES = {
    "quick": "quick",
    "medium": "medium",
    "thorough": "thorough",
    "very-thorough": "thorough",
    "very thorough": "thorough",
}


def normalize_depth(value: str) -> str:
    key = (value or "medium").strip().lower()
    if key not in DEPTH_ALIASES:
        raise SystemExit("Unknown depth %r (quick | medium | thorough)" % value)
    return DEPTH_ALIASES[key]


class Config:
    def __init__(self) -> None:
        self.engine = engine_root()
        self.repo = Path.cwd()
        self.code_root = ""
        self.test_root = ""
        self.test_command = ""
        self.roles: Dict[str, str] = {name: spec["default"] for name, spec in ROLES.items()}
        self.skip: List[str] = []
        self.phase_timeout = 1800
        self.fake = False
        self.dry_run = False
        self.force = False
        self.stop_after: Optional[str] = None
        self.depth = "medium"
        self.role_overrides: set = set()
        # Past-commits (unscoped / --since) reviewer. One runtime. PR/feature use roles["reviewer"].
        self.range_reviewer = "grok"

    def assignment(self, role: str) -> str:
        if self.fake:
            if role == "tester" and self.roles.get(role) == "host":
                return "host"
            return "fake"
        return self.roles[role]


def default_roles() -> Dict[str, str]:
    return {name: spec["default"] for name, spec in ROLES.items()}


def resolve_phase(name: str) -> str:
    key = name.strip().lower()
    return PHASE_ALIASES.get(key, key)


def load_config(
    repo: Path,
    *,
    assign: Optional[Iterable[str]] = None,
    skip: Optional[Iterable[str]] = None,
    fake: bool = False,
    dry_run: bool = False,
    force: bool = False,
    stop_after: Optional[str] = None,
    code_root: str = "",
    test_root: str = "",
    test_command: str = "",
    depth: str = "",
) -> Config:
    cfg = Config()
    cfg.repo = repo.resolve()
    cfg.fake = fake
    cfg.dry_run = dry_run
    cfg.force = force
    if stop_after:
        cfg.stop_after = resolve_phase(stop_after)
    if depth:
        cfg.depth = normalize_depth(depth)

    files = [
        cfg.engine / "config.toml",
        cfg.repo / ".team" / "config.toml",
    ]
    for path in files:
        if path.is_file():
            _apply_toml(cfg, parse_simple_toml(path.read_text(encoding="utf-8")))

    if code_root:
        cfg.code_root = code_root
    if test_root:
        cfg.test_root = test_root
    if test_command:
        cfg.test_command = test_command
    if assign:
        for item in assign:
            role, runtime = _parse_assign(item)
            _apply_assign(cfg, role, runtime)
    if skip:
        for item in skip:
            for part in str(item).split(","):
                part = part.strip()
                if part:
                    cfg.skip.append(resolve_phase(part))

    env_skip = os.environ.get("TEAM_SKIP", "")
    if env_skip:
        cfg.skip.extend(resolve_phase(p) for p in env_skip.split(",") if p.strip())

    timeout = os.environ.get("TEAM_PHASE_TIMEOUT")
    if timeout:
        cfg.phase_timeout = int(timeout)

    return cfg


def _apply_assign(cfg: Config, role: str, runtime: str) -> None:
    """``all=grok`` / ``*=claude`` sets every role that allows that runtime.

    Later ``--assign reviewer=claude`` still wins for that one role.
    """
    key = (role or "").strip().lower()
    if key in ("all", "*", "every"):
        if not runtime:
            raise SystemExit("Assignment must be all=runtime, got empty runtime")
        matched = False
        for name, spec in ROLES.items():
            allowed = spec["runtimes"]
            if runtime in allowed or runtime == "fake":
                cfg.roles[name] = runtime
                cfg.role_overrides.add(name)
                matched = True
        if runtime in ("claude", "grok"):
            cfg.range_reviewer = runtime
        if not matched:
            raise SystemExit(
                "No role accepts runtime %s (try claude, grok, both, host)" % runtime
            )
        return
    _set_role(cfg, role, runtime)
    cfg.role_overrides.add(role)


def _set_role(cfg: Config, role: str, runtime: str) -> None:
    if role not in ROLES:
        raise SystemExit(
            "Unknown role: %s (choose %s, or all=<runtime>)"
            % (role, ", ".join(ROLES))
        )
    allowed = ROLES[role]["runtimes"]
    if runtime not in allowed and runtime != "fake":
        raise SystemExit(
            "Role %s cannot use runtime %s (allowed: %s)"
            % (role, runtime, ", ".join(allowed))
        )
    cfg.roles[role] = runtime


def _parse_assign(item: str) -> Tuple[str, str]:
    if "=" not in item:
        raise SystemExit("Assignment must be role=runtime, got %r" % item)
    role, runtime = item.split("=", 1)
    return role.strip(), runtime.strip()


_PATH_KEYS = ("code_root", "test_root", "test_command")
_CONFIG_ALIASES = {
    "code_root": ("paths", "code_root", "str"),
    "test_root": ("paths", "test_root", "str"),
    "test_command": ("paths", "test_command", "str"),
    "skip": ("run", "skip", "list"),
    "phase_timeout": ("run", "phase_timeout", "int"),
    "range_reviewer": ("review", "range_reviewer", "str"),
}
_UNSET_DEFAULTS = {
    ("paths", "code_root"): "",
    ("paths", "test_root"): "",
    ("paths", "test_command"): "",
    ("run", "skip"): [],
    ("run", "phase_timeout"): 1800,
    ("review", "range_reviewer"): "grok",
}
_KEY_LINE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)(\s*=\s*)(.*)$")
TomlUpdate = Tuple[str, str, Any]
TomlDelete = Tuple[str, str]


def config_path(repo: Path) -> Path:
    return repo.resolve() / ".team" / "config.toml"


def resolve_config_key(name: str) -> Tuple[str, str, str]:
    """Return (section, key, kind) for a file-backed option. kind is str|int|list|role."""
    raw = (name or "").strip()
    if not raw:
        raise SystemExit("empty config key")
    if "." in raw:
        section, rest = raw.split(".", 1)
        section = section.strip()
        rest = rest.strip()
        if section == "paths":
            key = rest.replace("-", "_")
            if key in _PATH_KEYS:
                return "paths", key, "str"
        elif section == "run":
            key = rest.replace("-", "_")
            if key == "skip":
                return "run", "skip", "list"
            if key == "phase_timeout":
                return "run", "phase_timeout", "int"
        elif section == "review":
            key = rest.replace("-", "_")
            if key == "range_reviewer":
                return "review", "range_reviewer", "str"
        elif section == "roles":
            role = rest.replace("_", "-")
            if role in ROLES:
                return "roles", role, "role"
        raise SystemExit("Unknown config key %r" % name)
    role = raw.replace("_", "-")
    if role in ROLES:
        return "roles", role, "role"
    alias = raw.replace("-", "_")
    if alias in _CONFIG_ALIASES:
        return _CONFIG_ALIASES[alias]
    raise SystemExit(
        "Unknown config key %r (paths, skip, phase_timeout, range_reviewer, or a role)"
        % name
    )


def parse_config_value(kind: str, raw: str) -> Any:
    if kind == "int":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise SystemExit("phase_timeout must be an integer, got %r" % raw)
    if kind == "list":
        text = str(raw).strip()
        if text.startswith("[") and text.endswith("]"):
            inner = text[1:-1].strip()
            if not inner:
                return []
            return [p.strip().strip("\"'") for p in inner.split(",") if p.strip()]
        if not text:
            return []
        return [p.strip() for p in text.split(",") if p.strip()]
    return raw


def unset_config_value(section: str, key: str, kind: str) -> Any:
    if kind == "role":
        raise SystemExit("unset %s by deleting the role key" % key)
    if (section, key) not in _UNSET_DEFAULTS:
        raise SystemExit("cannot unset %s.%s" % (section, key))
    return _UNSET_DEFAULTS[(section, key)]


def flatten_skip(items: Sequence[str]) -> List[str]:
    out: List[str] = []
    for item in items:
        for part in str(item).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def collect_config_edits(
    *,
    pairs: Sequence[str] = (),
    unsets: Sequence[str] = (),
    code_root: Optional[str] = None,
    test_root: Optional[str] = None,
    test_command: Optional[str] = None,
    assign: Sequence[str] = (),
    skip: Optional[Sequence[str]] = None,
    range_reviewer: Optional[str] = None,
    phase_timeout: Optional[int] = None,
) -> Tuple[List[TomlUpdate], List[TomlDelete]]:
    """Build validated file edits. Later inputs win (pairs last)."""
    updates: Dict[Tuple[str, str], Any] = {}
    deletes = set()

    def put(section: str, key: str, kind: str, value: Any) -> None:
        deletes.discard((section, key))
        updates[(section, key)] = validate_config_update(section, key, kind, value)

    def drop(section: str, key: str, kind: str) -> None:
        updates.pop((section, key), None)
        if kind == "role":
            deletes.add((section, key))
            return
        updates[(section, key)] = unset_config_value(section, key, kind)

    for name in unsets:
        section, key, kind = resolve_config_key(name)
        drop(section, key, kind)
    if code_root is not None:
        put("paths", "code_root", "str", code_root)
    if test_root is not None:
        put("paths", "test_root", "str", test_root)
    if test_command is not None:
        put("paths", "test_command", "str", test_command)
    for item in assign:
        for section, key, value in updates_from_assign(item):
            kind = "role" if section == "roles" else "str"
            put(section, key, kind, value)
    if skip is not None:
        put("run", "skip", "list", flatten_skip(skip))
    if range_reviewer is not None:
        put("review", "range_reviewer", "str", range_reviewer)
    if phase_timeout is not None:
        put("run", "phase_timeout", "int", phase_timeout)
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit("expected KEY=VALUE, got %r" % pair)
        name, raw = pair.split("=", 1)
        section, key, kind = resolve_config_key(name)
        put(section, key, kind, parse_config_value(kind, raw))
    return (
        [(section, key, value) for (section, key), value in updates.items()],
        sorted(deletes),
    )


def validate_config_update(section: str, key: str, kind: str, value: Any) -> Any:
    if kind == "role":
        probe = Config()
        _set_role(probe, key, str(value))
        return probe.roles[key]
    if section == "review" and key == "range_reviewer":
        runtime = str(value).strip()
        if runtime not in ("claude", "grok"):
            raise SystemExit("review.range_reviewer must be claude or grok (one reviewer)")
        return runtime
    if section == "run" and key == "phase_timeout":
        try:
            timeout = int(value)
        except (TypeError, ValueError):
            raise SystemExit("phase_timeout must be an integer, got %r" % value)
        if timeout < 0:
            raise SystemExit("phase_timeout must be >= 0")
        return timeout
    if section == "run" and key == "skip":
        if not isinstance(value, list):
            value = parse_config_value("list", str(value))
        return [resolve_phase(str(item)) for item in value if str(item).strip()]
    if kind == "str":
        return "" if value is None else str(value)
    return value


def updates_from_assign(item: str) -> List[TomlUpdate]:
    role, runtime = _parse_assign(item)
    probe = Config()
    _apply_assign(probe, role, runtime)
    out: List[TomlUpdate] = []
    for name in sorted(probe.role_overrides):
        out.append(("roles", name, probe.roles[name]))
    key = (role or "").strip().lower()
    if key in ("all", "*", "every") and runtime in ("claude", "grok"):
        out.append(("review", "range_reviewer", runtime))
    return out


def format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(format_toml_value(item) for item in value) + "]"
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def update_simple_toml(text: str, updates: Sequence[TomlUpdate]) -> str:
    """Apply (section, key, value) updates. Preserve comments and unknown keys."""
    out = text or ""
    for section, key, value in updates:
        out = _upsert_toml_key(out, section, key, value)
    if out and not out.endswith("\n"):
        out += "\n"
    return out


def delete_toml_key(text: str, section: str, key: str) -> str:
    lines = (text or "").splitlines()
    current = "default"
    drop: Optional[int] = None
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            continue
        match = _KEY_LINE.match(raw)
        if match and current == section and match.group(2) == key:
            drop = i
            break
    if drop is None:
        out = text or ""
        return out if not out or out.endswith("\n") else out + "\n"
    del lines[drop]
    out = "\n".join(lines)
    if out and not out.endswith("\n"):
        out += "\n"
    return out


def apply_config_edits(
    text: str,
    *,
    updates: Sequence[TomlUpdate] = (),
    deletes: Sequence[TomlDelete] = (),
) -> str:
    out = text or ""
    for section, key in deletes:
        out = delete_toml_key(out, section, key)
    return update_simple_toml(out, updates)


def seed_config_text() -> str:
    example = engine_root() / "config.example.toml"
    if example.is_file():
        return example.read_text(encoding="utf-8")
    return ""


def write_config_file(
    path: Path,
    *,
    updates: Sequence[TomlUpdate] = (),
    deletes: Sequence[TomlDelete] = (),
    seed_if_missing: bool = True,
) -> str:
    if path.is_file():
        text = path.read_text(encoding="utf-8")
    elif seed_if_missing:
        text = seed_config_text()
    else:
        text = ""
    body = apply_config_edits(text, updates=updates, deletes=deletes)
    write_text(path, body)
    return body


def _split_value_and_comment(rest: str) -> Tuple[str, str]:
    in_str = False
    quote = ""
    escape = False
    for i, char in enumerate(rest):
        if in_str:
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == quote:
                in_str = False
            continue
        if char in ('"', "'"):
            in_str = True
            quote = char
            continue
        if char == "#":
            return rest[:i].rstrip(), rest[i:]
    return rest.rstrip(), ""


def _upsert_toml_key(text: str, section: str, key: str, value: Any) -> str:
    formatted = format_toml_value(value)
    ended_nl = bool(text) and text.endswith("\n")
    lines = (text or "").splitlines()
    current = "default"
    section_start: Optional[int] = None
    last_key_in_section: Optional[int] = None
    key_line: Optional[int] = None
    next_section: Optional[int] = None
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1].strip()
            if current == section and name != section and next_section is None:
                next_section = i
            current = name
            if name == section and section_start is None:
                section_start = i
            continue
        match = _KEY_LINE.match(raw)
        if not match:
            continue
        if current == section:
            last_key_in_section = i
            if match.group(2) == key:
                key_line = i

    if key_line is not None:
        raw = lines[key_line]
        match = _KEY_LINE.match(raw)
        assert match is not None
        prefix = match.group(1) + match.group(2) + match.group(3)
        _old, comment = _split_value_and_comment(match.group(4))
        if comment:
            lines[key_line] = prefix + formatted + "  " + comment.lstrip()
        else:
            lines[key_line] = prefix + formatted
        out = "\n".join(lines)
        return out + "\n" if ended_nl or out else out

    new_line = "%s = %s" % (key, formatted)
    if section_start is not None:
        if last_key_in_section is not None:
            insert_at = last_key_in_section + 1
        elif next_section is not None:
            insert_at = next_section
            while insert_at > section_start + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
        else:
            insert_at = section_start + 1
            for j in range(section_start + 1, len(lines)):
                look = lines[j].strip()
                if look.startswith("[") and look.endswith("]"):
                    break
                if look:
                    insert_at = j + 1
        lines.insert(insert_at, new_line)
        out = "\n".join(lines)
        return out + "\n" if ended_nl or out else out

    out = text or ""
    if out and not out.endswith("\n"):
        out += "\n"
    if out.strip():
        out += "\n"
    out += "[%s]\n%s\n" % (section, new_line)
    return out


def parse_simple_toml(text: str) -> Dict[str, Dict[str, Any]]:
    """Minimal TOML: [section], key = \"str\" | true | false | int | [list]."""
    sections: Dict[str, Dict[str, Any]] = {}
    current = "default"
    sections[current] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            sections.setdefault(current, {})
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        sections[current][key.strip()] = _parse_toml_value(val.strip())
    return sections


def _parse_toml_value(val: str) -> Any:
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_parse_toml_value(p.strip()) for p in inner.split(",")]
    if (val.startswith('"') and val.endswith('"')) or (
        val.startswith("'") and val.endswith("'")
    ):
        return val[1:-1]
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    try:
        return int(val)
    except ValueError:
        return val


def _apply_toml(cfg: Config, data: Dict[str, Dict[str, Any]]) -> None:
    paths = data.get("paths") or {}
    if paths.get("code_root"):
        cfg.code_root = str(paths["code_root"])
    if paths.get("test_root"):
        cfg.test_root = str(paths["test_root"])
    if paths.get("test_command"):
        cfg.test_command = str(paths["test_command"])
    roles = data.get("roles") or {}
    for role, runtime in roles.items():
        _set_role(cfg, str(role), str(runtime))
    run = data.get("run") or {}
    if isinstance(run.get("skip"), list):
        cfg.skip.extend(resolve_phase(str(x)) for x in run["skip"])
    if run.get("phase_timeout") is not None:
        cfg.phase_timeout = int(run["phase_timeout"])
    review = data.get("review") or {}
    if review.get("range_reviewer"):
        runtime = str(review["range_reviewer"]).strip()
        if runtime not in ("claude", "grok"):
            raise SystemExit("review.range_reviewer must be claude or grok (one reviewer)")
        cfg.range_reviewer = runtime


def persona_path(role: str) -> Path:
    name = {
        "tdd-design": "tdd-design.md",
        "test-writer": "test-writer.md",
    }.get(role, role + ".md")
    return engine_root() / "personas" / name


def schema_path(name: str) -> Path:
    return engine_root() / "schemas" / name


def deepcopy_roles(roles: Dict[str, str]) -> Dict[str, str]:
    return deepcopy(roles)


def apply_range_reviewer(cfg: Config, *, pr: bool, reviewer: str = "") -> None:
    """PR uses both unless overridden. Past-commits uses one runtime (default grok).

    ``reviewer`` is ``team review --reviewer claude|grok``. Same effect as
    ``--assign reviewer=…`` but sits on the review command.
    """
    forced = (reviewer or "").strip()
    if forced:
        if forced not in ("claude", "grok"):
            raise SystemExit("review --reviewer must be claude or grok (one reviewer)")
        cfg.roles["reviewer"] = forced
        cfg.role_overrides.add("reviewer")
    if pr:
        if "reviewer" not in cfg.role_overrides:
            cfg.roles["reviewer"] = "both"
        return
    if "reviewer" in cfg.role_overrides:
        runtime = cfg.roles.get("reviewer")
        if runtime == "both":
            raise SystemExit(
                "past-commits review uses one reviewer; "
                "use team review --reviewer claude (or grok)"
            )
        return
    cfg.roles["reviewer"] = cfg.range_reviewer
