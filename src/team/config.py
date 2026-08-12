from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from team.util import engine_root

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
            _set_role(cfg, role, runtime)
            cfg.role_overrides.add(role)
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


def _set_role(cfg: Config, role: str, runtime: str) -> None:
    if role not in ROLES:
        raise SystemExit("Unknown role: %s (choose %s)" % (role, ", ".join(ROLES)))
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
