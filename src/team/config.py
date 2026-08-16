from __future__ import annotations

import os
import re
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from team import gitutil
from team.util import engine_root, normalize_root, write_text

# Shipped headless coding-agent adapters. Interchangeable: any role that
# accepts one accepts the others. A third CLI is another adapter (register
# + this tuple if it is a shipped peer), not a phase fork.
CODING_RUNTIMES = ("claude", "grok")

# Reviewer (and review.range_reviewer) may be one runtime or every shipped
# coding runtime in parallel. ``both`` expands via expand_reviewer().
REVIEWER_RUNTIMES = CODING_RUNTIMES + ("both",)

# Only these capabilities may edit the product tree. Every other persona
# (read-only inspect, execute/tester) is non-write: no write tools, inspect fence.
WRITE_CAPABILITIES = frozenset(("write-tests", "write-code"))


def may_write(capability: str) -> bool:
    return (capability or "") in WRITE_CAPABILITIES


# role -> default runtime, allowed runtimes, capability
ROLES: Dict[str, Dict[str, Any]] = {
    "architect": {
        "default": "claude",
        "runtimes": CODING_RUNTIMES,
        "capability": "read-only",
        "optional": False,
    },
    "critic": {
        "default": "claude",
        "runtimes": CODING_RUNTIMES,
        "capability": "read-only",
        "optional": True,
    },
    "tdd-design": {
        "default": "claude",
        "runtimes": CODING_RUNTIMES,
        "capability": "read-only",
        "optional": False,
    },
    "test-writer": {
        "default": "grok",
        "runtimes": CODING_RUNTIMES,
        "capability": "write-tests",
        "optional": False,
    },
    "implementer": {
        "default": "grok",
        "runtimes": CODING_RUNTIMES,
        "capability": "write-code",
        "optional": False,
    },
    "tester": {
        "default": "host",
        "runtimes": ("host",) + CODING_RUNTIMES,
        "capability": "execute",
        "optional": False,
    },
    "adversarial": {
        "default": "grok",
        "runtimes": CODING_RUNTIMES,
        "capability": "write-tests",
        "optional": True,
    },
    "debugger": {
        "default": "claude",
        "runtimes": CODING_RUNTIMES,
        "capability": "read-only",
        "optional": True,
    },
    "reviewer": {
        "default": "both",
        "runtimes": REVIEWER_RUNTIMES,
        "capability": "read-only",
        "optional": False,
    },
    "guardian": {
        "default": "claude",
        "runtimes": CODING_RUNTIMES,
        "capability": "read-only",
        "optional": True,
    },
    "scout": {
        "default": "grok",
        "runtimes": CODING_RUNTIMES,
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

# The phases a user may turn off. Every other phase name is either
# structural (the rail collapses without it) or not a phase at all. This is
# the one home: pipeline._skip_reason honours this set, and the CLI, the env
# var and [run] skip are all validated against it, so `--skip reviewer` is an
# error instead of a flag that prints nothing and runs the reviewer anyway.
OPTIONAL_PHASES = (
    "critic",
    "adversarial",
    "guardian",
    "debugger",
    "repair",
    "verify-test",
    "adversarial-test",
)

AUDIT_PHASE_ORDER = [
    "scout",
    "assess",
    "reviewer",
]

RANGE_PHASE_ORDER = [
    "reviewer",
    "guardian",
]

DEFAULT_RANGE_SLUG = "review-since-tag"

DEFAULT_AUDIT_QUERY = "Status of this workspace: WIP, finished, missing"

DEPTH_ALIASES = {
    "quick": "quick",
    "medium": "medium",
    "thorough": "thorough",
    "very-thorough": "thorough",
    "very thorough": "thorough",
}

# Effort is a runtime-neutral integer, 0 (lowest) .. 5 (highest). Not a runtime
# name, not audit --depth. Vendors disagree on both the names and the number of
# rungs -- Claude has five (low..max), Grok four (low..xhigh) -- so the config
# layer must not speak either vocabulary. ``runners`` owns the translation and
# snaps a level the runtime does not implement to its nearest rung.
EFFORT_MIN = 0
EFFORT_MAX = 5

# Legacy spellings, kept because they are the words both CLIs still use. They
# name positions on the neutral scale; they are not the value we store.
EFFORT_ALIASES = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "xhigh": 4,
    "max": 5,
}

# Reasoning roles only. 3 is the former "high" default, unchanged.
DEFAULT_EFFORT = {
    "architect": 3,
    "critic": 3,
    "reviewer": 3,
    "guardian": 3,
    "tdd-design": 3,
}


def normalize_depth(value: str) -> str:
    key = (value or "medium").strip().lower()
    if key not in DEPTH_ALIASES:
        raise SystemExit("Unknown depth %r (quick | medium | thorough)" % value)
    return DEPTH_ALIASES[key]


def normalize_effort(value: Any) -> int:
    """Any accepted spelling -> the neutral 0..5 integer we store.

    Names are accepted because both CLIs and every existing config file use
    them, but they are translated on the way in: one representation reaches the
    rest of the program, so no caller has to know a vendor's ladder.
    """
    key = str(value if value is not None else "").strip().lower()
    if key in EFFORT_ALIASES:
        return EFFORT_ALIASES[key]
    try:
        level = int(key)
    except (TypeError, ValueError):
        raise SystemExit(
            "Unknown effort %r (%d..%d, or %s)"
            % (value, EFFORT_MIN, EFFORT_MAX, " | ".join(EFFORT_ALIASES))
        )
    if not EFFORT_MIN <= level <= EFFORT_MAX:
        raise SystemExit(
            "Effort %r out of range (%d is lowest, %d is highest)"
            % (value, EFFORT_MIN, EFFORT_MAX)
        )
    return level


class Config:
    def __init__(self) -> None:
        self.engine = engine_root()
        self.repo = Path.cwd()
        self.code_root = ""
        self.test_root = ""
        self.test_command = ""
        self.lock_code_root = False
        self.lock_test_root = False
        self.roles: Dict[str, str] = {name: spec["default"] for name, spec in ROLES.items()}
        self.skip: List[str] = []
        self.phase_timeout = 1800
        # Bytes of patch a hop is handed. Not a fence: the write fence still
        # sees every dirty path. This caps what a reader is asked to ingest,
        # because a hop pays its context on every turn.
        self.diff_budget = gitutil.DIFF_BUDGET
        self.fake = False
        self.dry_run = False
        self.force = False
        self.stop_after: Optional[str] = None
        self.depth = "medium"
        self.role_overrides: set = set()
        # Per-role effort, on the neutral 0..5 scale. Not a runtime name.
        self.effort: Dict[str, int] = {}
        # Past-commits (unscoped / --since) default. A coding runtime or both.
        # PR and feature use roles["reviewer"] unless --reviewer / --assign overrides.
        self.range_reviewer = "grok"

    def assignment(self, role: str) -> str:
        if self.fake:
            if role == "tester" and self.roles.get(role) == "host":
                return "host"
            return "fake"
        return self.roles[role]

    def effort_for(self, role: str) -> Optional[int]:
        """Resolved neutral effort for this role. None means omit the flag."""
        if role in self.effort:
            return self.effort[role]
        return DEFAULT_EFFORT.get(role)


def default_roles() -> Dict[str, str]:
    return {name: spec["default"] for name, spec in ROLES.items()}


def resolve_phase(name: str) -> str:
    key = name.strip().lower()
    return PHASE_ALIASES.get(key, key)


def resolve_skip(name: str) -> str:
    """Phase name for --skip / TEAM_SKIP / [run] skip, or exit saying why not."""
    phase = resolve_phase(name)
    if phase not in OPTIONAL_PHASES:
        raise SystemExit(
            "cannot skip %r (optional phases: %s)"
            % (name, ", ".join(OPTIONAL_PHASES))
        )
    return phase


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
    effort: Optional[Iterable[str]] = None,
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
            _apply_toml(cfg, read_config_file(path))

    if code_root:
        cfg.code_root = normalize_root(code_root)
        cfg.lock_code_root = True
    if test_root:
        cfg.test_root = normalize_root(test_root)
        cfg.lock_test_root = True
    if test_command:
        cfg.test_command = test_command
    if assign:
        for item in assign:
            role, runtime = _parse_assign(item)
            _apply_assign(cfg, role, runtime)
    if effort:
        for item in effort:
            role, level = _parse_effort(item)
            _apply_effort(cfg, role, level)
    if skip:
        for item in skip:
            for part in str(item).split(","):
                part = part.strip()
                if part:
                    cfg.skip.append(resolve_skip(part))

    env_skip = os.environ.get("TEAM_SKIP", "")
    if env_skip:
        cfg.skip.extend(resolve_skip(p) for p in env_skip.split(",") if p.strip())

    timeout = os.environ.get("TEAM_PHASE_TIMEOUT")
    if timeout:
        cfg.phase_timeout = int(timeout)

    budget = os.environ.get("TEAM_DIFF_BUDGET")
    if budget:
        cfg.diff_budget = int(budget)

    return cfg


def read_config_file(path: Path) -> Dict[str, Dict[str, Any]]:
    """parse_simple_toml with the file named. An unreadable config stops the run."""
    try:
        return parse_simple_toml(path.read_text(encoding="utf-8"))
    except TomlError as exc:
        raise SystemExit("%s is not readable TOML: %s" % (path, exc))


def expand_reviewer(assignment: str) -> List[str]:
    """Concrete reviewer runtimes. ``both`` is every shipped coding adapter."""
    name = (assignment or "").strip()
    if name == "both":
        return list(CODING_RUNTIMES)
    if not name:
        return []
    return [name]


def is_model_runtime(name: str) -> bool:
    """True for a headless coding-agent adapter (shipped, fake, or registered).

    ``host`` and the ``both`` alias are not adapters.
    """
    runtime = (name or "").strip()
    if runtime in ("", "host", "both"):
        return False
    if runtime == "fake" or runtime in CODING_RUNTIMES:
        return True
    return _is_registered_runtime(runtime)


def role_accepts_runtime(role: str, runtime: str) -> bool:
    """Whether this role may run on this adapter.

    Shipped coding runtimes are interchangeable. A registered third
    adapter is accepted on any role that already accepts the shipped pair.
    """
    if role not in ROLES:
        return False
    allowed = ROLES[role]["runtimes"]
    if runtime in allowed or runtime == "fake":
        return True
    if runtime in ("host", "both"):
        return runtime in allowed
    if not _is_registered_runtime(runtime):
        return False
    return any(name in allowed for name in CODING_RUNTIMES)


def _is_registered_runtime(name: str) -> bool:
    from team.runners import is_registered

    return is_registered(name)


def _apply_assign(cfg: Config, role: str, runtime: str) -> None:
    """``all=grok`` / ``*=claude`` sets every role that allows that runtime.

    Later ``--assign reviewer=claude`` still wins for that one role.
    """
    key = (role or "").strip().lower()
    if key in ("all", "*", "every"):
        if not runtime:
            raise SystemExit("Assignment must be all=runtime, got empty runtime")
        matched = False
        for name in ROLES:
            if role_accepts_runtime(name, runtime):
                cfg.roles[name] = runtime
                cfg.role_overrides.add(name)
                matched = True
        if runtime in REVIEWER_RUNTIMES:
            cfg.range_reviewer = runtime
        if not matched:
            raise SystemExit(
                "No role accepts runtime %s (try %s, host, or a registered adapter)"
                % (runtime, ", ".join(CODING_RUNTIMES + ("both",)))
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
    if not role_accepts_runtime(role, runtime):
        raise SystemExit(
            "Role %s cannot use runtime %s (allowed: %s)"
            % (role, runtime, ", ".join(ROLES[role]["runtimes"]))
        )
    cfg.roles[role] = runtime


def _parse_assign(item: str) -> Tuple[str, str]:
    if "=" not in item:
        raise SystemExit("Assignment must be role=runtime, got %r" % item)
    role, runtime = item.split("=", 1)
    return role.strip(), runtime.strip()


def _parse_effort(item: str) -> Tuple[str, str]:
    if "=" not in item:
        raise SystemExit("Effort must be role=level, got %r" % item)
    role, level = item.split("=", 1)
    return role.strip(), level.strip()


def _apply_effort(cfg: Config, role: str, level: str) -> None:
    """``all=xhigh`` sets every role. Later ``--effort implementer=medium`` wins."""
    key = (role or "").strip().lower()
    if key in ("all", "*", "every"):
        value = normalize_effort(level)
        for name in ROLES:
            cfg.effort[name] = value
        return
    _set_effort(cfg, role, level)


def _set_effort(cfg: Config, role: str, level: str) -> None:
    name = (role or "").strip().lower().replace("_", "-")
    if name not in ROLES:
        raise SystemExit(
            "Unknown role: %s (choose %s, or all=<level>)"
            % (role, ", ".join(ROLES))
        )
    cfg.effort[name] = normalize_effort(level)


_PATH_KEYS = ("code_root", "test_root", "test_command")
_CONFIG_ALIASES = {
    "code_root": ("paths", "code_root", "str"),
    "test_root": ("paths", "test_root", "str"),
    "test_command": ("paths", "test_command", "str"),
    "skip": ("run", "skip", "list"),
    "phase_timeout": ("run", "phase_timeout", "int"),
    "diff_budget": ("run", "diff_budget", "int"),
    "range_reviewer": ("review", "range_reviewer", "str"),
}
_UNSET_DEFAULTS = {
    ("paths", "code_root"): "",
    ("paths", "test_root"): "",
    ("paths", "test_command"): "",
    ("run", "skip"): [],
    ("run", "phase_timeout"): 1800,
    ("run", "diff_budget"): gitutil.DIFF_BUDGET,
    ("review", "range_reviewer"): "grok",
}
_KEY_LINE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)(\s*=\s*)(.*)$")
TomlUpdate = Tuple[str, str, Any]
TomlDelete = Tuple[str, str]


def config_path(repo: Path) -> Path:
    return repo.resolve() / ".team" / "config.toml"


def resolve_config_key(name: str) -> Tuple[str, str, str]:
    """Return (section, key, kind) for a file-backed option. kind is str|int|list|role|effort."""
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
            if key == "diff_budget":
                return "run", "diff_budget", "int"
        elif section == "review":
            key = rest.replace("-", "_")
            if key == "range_reviewer":
                return "review", "range_reviewer", "str"
        elif section == "roles":
            role = rest.replace("_", "-")
            if role in ROLES:
                return "roles", role, "role"
        elif section == "effort":
            role = rest.replace("_", "-")
            if role in ROLES:
                return "effort", role, "effort"
        raise SystemExit("Unknown config key %r" % name)
    role = raw.replace("_", "-")
    if role in ROLES:
        return "roles", role, "role"
    alias = raw.replace("-", "_")
    if alias in _CONFIG_ALIASES:
        return _CONFIG_ALIASES[alias]
    raise SystemExit(
        "Unknown config key %r (paths, skip, phase_timeout, diff_budget, "
        "range_reviewer, effort.<role>, or a role)"
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
    diff_budget: Optional[int] = None,
    effort: Sequence[str] = (),
) -> Tuple[List[TomlUpdate], List[TomlDelete]]:
    """Build validated file edits. Later inputs win (pairs last)."""
    updates: Dict[Tuple[str, str], Any] = {}
    deletes = set()

    def put(section: str, key: str, kind: str, value: Any) -> None:
        deletes.discard((section, key))
        updates[(section, key)] = validate_config_update(section, key, kind, value)

    def drop(section: str, key: str, kind: str) -> None:
        updates.pop((section, key), None)
        if kind in ("role", "effort"):
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
    if diff_budget is not None:
        put("run", "diff_budget", "int", diff_budget)
    for item in effort:
        for section, key, value in updates_from_effort(item):
            put(section, key, "effort", value)
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
    if kind == "effort":
        probe = Config()
        _set_effort(probe, key, str(value))
        return probe.effort[key]
    if section == "review" and key == "range_reviewer":
        return normalize_reviewer(str(value), what="review.range_reviewer")
    if section == "run" and key == "diff_budget":
        try:
            budget = int(value)
        except (TypeError, ValueError):
            raise SystemExit("diff_budget must be an integer, got %r" % value)
        if budget < 0:
            raise SystemExit("diff_budget must be >= 0 (0 disables the cap)")
        return budget
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
        return [resolve_skip(str(item)) for item in value if str(item).strip()]
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
    if key in ("all", "*", "every") and runtime in REVIEWER_RUNTIMES:
        out.append(("review", "range_reviewer", runtime))
    return out


def updates_from_effort(item: str) -> List[TomlUpdate]:
    role, level = _parse_effort(item)
    probe = Config()
    _apply_effort(probe, role, level)
    return [("effort", name, probe.effort[name]) for name in sorted(probe.effort)]


# TOML basic-string escapes. A control character is not writable raw, so the
# whole set is spelled here rather than the two that happened to show up.
_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(format_toml_value(item) for item in value) + "]"
    out = []
    for char in str(value):
        if char in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[char])
        elif char < " " or char == "\x7f":
            out.append("\\u%04X" % ord(char))
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


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
    """Read the config file. TOML is parsed by the layer that owns the format.

    ``tomllib`` is the reader: quoting, escapes, and where a ``#`` starts a
    comment are its rules, not a second opinion here. The writer stays
    hand-rolled because it preserves comments and unknown keys, which
    ``tomllib`` cannot do -- so writer and reader are a seam, held by the
    round-trip contract test, not by two parsers agreeing by inspection.

    Shape is flattened to ``{section: {key: value}}``; a nested table keeps
    its dotted name. Top-level keys land under ``default``.
    """
    try:
        data = tomllib.loads(text or "")
    except tomllib.TOMLDecodeError as exc:
        raise TomlError(str(exc)) from exc
    sections: Dict[str, Dict[str, Any]] = {"default": {}}
    _flatten_tables(data, "", sections)
    return sections


class TomlError(ValueError):
    """Unreadable config file. Carries tomllib's own message."""


def _flatten_tables(
    table: Dict[str, Any], prefix: str, out: Dict[str, Dict[str, Any]]
) -> None:
    name = prefix or "default"
    out.setdefault(name, {})
    for key, value in table.items():
        if isinstance(value, dict):
            _flatten_tables(value, "%s.%s" % (prefix, key) if prefix else str(key), out)
            continue
        out[name][str(key)] = value


def _apply_toml(cfg: Config, data: Dict[str, Dict[str, Any]]) -> None:
    paths = data.get("paths") or {}
    if paths.get("code_root"):
        cfg.code_root = normalize_root(paths["code_root"])
        cfg.lock_code_root = True
    if paths.get("test_root"):
        cfg.test_root = normalize_root(paths["test_root"])
        cfg.lock_test_root = True
    if paths.get("test_command"):
        cfg.test_command = str(paths["test_command"])
    roles = data.get("roles") or {}
    for role, runtime in roles.items():
        _set_role(cfg, str(role), str(runtime))
    run = data.get("run") or {}
    if isinstance(run.get("skip"), list):
        cfg.skip.extend(resolve_skip(str(x)) for x in run["skip"])
    if run.get("phase_timeout") is not None:
        cfg.phase_timeout = int(run["phase_timeout"])
    if run.get("diff_budget") is not None:
        cfg.diff_budget = int(run["diff_budget"])
    review = data.get("review") or {}
    if review.get("range_reviewer"):
        cfg.range_reviewer = normalize_reviewer(
            str(review["range_reviewer"]), what="review.range_reviewer"
        )
    effort = data.get("effort") or {}
    for role, level in effort.items():
        text = "" if level is None else str(level).strip()
        if not text:
            continue
        _set_effort(cfg, str(role), text)


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


def normalize_reviewer(value: str, *, what: str) -> str:
    runtime = (value or "").strip()
    if runtime not in REVIEWER_RUNTIMES:
        raise SystemExit(
            "%s must be %s" % (what, ", ".join(REVIEWER_RUNTIMES))
        )
    return runtime


def apply_range_reviewer(cfg: Config, *, pr: bool, reviewer: str = "") -> None:
    """PR defaults to both. Past-commits defaults to range_reviewer (grok).

    ``reviewer`` is ``team review --reviewer`` plus a shipped coding
    runtime or ``both``. Same effect as ``--assign reviewer=…``. ``both``
    runs every name in ``CODING_RUNTIMES`` in parallel.
    """
    forced = (reviewer or "").strip()
    if forced:
        cfg.roles["reviewer"] = normalize_reviewer(
            forced, what="review --reviewer"
        )
        cfg.role_overrides.add("reviewer")
    if pr:
        if "reviewer" not in cfg.role_overrides:
            cfg.roles["reviewer"] = "both"
        return
    if "reviewer" in cfg.role_overrides:
        return
    cfg.roles["reviewer"] = cfg.range_reviewer
