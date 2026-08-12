from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from team.util import dump_json, load_json


@dataclass
class State:
    slug: str
    brief: str
    repo: str
    engine_root: str
    code_root: str = ""
    test_root: str = ""
    test_command: str = ""
    assignment: Dict[str, str] = field(default_factory=dict)
    phase: str = "architect"
    phases_done: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    sessions: Dict[str, str] = field(default_factory=dict)
    git: Dict[str, Any] = field(default_factory=dict)
    baseline: Dict[str, Any] = field(default_factory=dict)
    final: Dict[str, Any] = field(default_factory=dict)
    stop_reason: str = ""
    mode: str = "feature"
    depth: str = "medium"
    diagnosis_owner: str = ""
    adversarial_run: Dict[str, Any] = field(default_factory=dict)
    range_base: str = ""
    range_kind: str = ""
    range_pr: str = ""
    stamp_tag: str = ""

    def mark(self, phase: str) -> None:
        if phase not in self.phases_done:
            self.phases_done.append(phase)
        self.phase = phase

    def rewind_to(self, phase: str, order: List[str]) -> None:
        if phase not in order:
            return
        keep = set(order[: order.index(phase)])
        self.phases_done = [p for p in self.phases_done if p in keep]
        self.skipped = [p for p in self.skipped if p in keep]
        self.phase = phase

    def save(self, work: Path) -> None:
        dump_json(work / "state.json", asdict(self))

    @classmethod
    def load(cls, work: Path) -> "State":
        data = load_json(work / "state.json")
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: data[k] for k in known if k in data})


def work_dir(repo: Path, slug: str) -> Path:
    return repo / ".team" / "work" / slug


def require_work(repo: Path, slug: str) -> Path:
    work = work_dir(repo, slug)
    if not (work / "state.json").is_file():
        raise SystemExit("No run at %s (missing state.json)" % work)
    return work
