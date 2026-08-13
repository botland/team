from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import hashlib

from team.util import as_list, as_str, dump_json, load_json


class FindingsError(RuntimeError):
    pass

KINDS = ("architecture", "implementation", "test", "note", "unclassified")

KIND_ALIASES = {
    "architecture": "architecture",
    "architect": "architecture",
    "design": "architecture",
    "structural": "architecture",
    "invariant": "architecture",
    "implementation": "implementation",
    "implementer": "implementation",
    "impl": "implementation",
    "code": "implementation",
    "bug": "implementation",
    "production": "implementation",
    "test": "test",
    "tests": "test",
    "tdd": "test",
    "tdd-design": "test",
    "test-writer": "test",
    "contract": "test",
    "note": "note",
    "info": "note",
    "open": "note",
    "n/a": "note",
    "na": "note",
}

ACTIONABLE = frozenset(("architecture", "implementation", "test"))

CONSOLE_LIMIT = 10

_SEVERITY_RANK = {
    "critical": 0,
    "high": 1,
    "invariant": 2,
    "medium": 3,
    "low": 4,
}

_KIND_RANK = {
    "implementation": 0,
    "test": 1,
    "architecture": 2,
    "note": 3,
}


def normalize_kind(value: Any) -> str:
    key = as_str(value).strip().lower()
    if not key:
        return "unclassified"
    return KIND_ALIASES.get(key, "unclassified")


def normalize_finding(item: Dict[str, Any], *, source: str = "") -> Dict[str, Any]:
    kind = normalize_kind(item.get("kind"))
    out = {
        "severity": as_str(item.get("severity")) or "?",
        "title": as_str(item.get("title")) or "(untitled)",
        "evidence": as_str(item.get("evidence")),
        "path": as_str(item.get("path")),
        "kind": kind,
        "source": source or as_str(item.get("source")),
    }
    return out


def collect_review_findings(work: Path) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    prompts = work / "prompts"
    if not prompts.is_dir():
        return found
    recorded = _recorded_review_results(work)
    if recorded is None:
        paths = sorted(prompts.glob("reviewer-*.result.json"))
    else:
        by_name = {spec["name"]: spec for spec in recorded}
        paths = []
        extras = []
        for path in sorted(prompts.glob("reviewer-*.result.json")):
            spec = by_name.get(path.name)
            if spec is None:
                extras.append(path.name)
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            expected = as_str(spec.get("digest"))
            if expected and digest != expected:
                raise FindingsError(
                    "digest mismatch for %s (recorded %s, file %s)"
                    % (path.name, expected, digest)
                )
            paths.append(path)
        for spec in recorded:
            name = as_str(spec.get("name"))
            if name and not (prompts / name).is_file():
                raise FindingsError("recorded review result missing: %s" % name)
    return _dedupe(_findings_from_result_paths(paths))


def collect_review_findings_unscoped(work: Path) -> List[Dict[str, Any]]:
    prompts = work / "prompts"
    if not prompts.is_dir():
        return []
    return _dedupe(_findings_from_result_paths(sorted(prompts.glob("reviewer-*.result.json"))))


def _findings_from_result_paths(paths: Iterable[Path]) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for path in paths:
        try:
            data = load_json(path)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for item in as_list(data.get("findings")):
            if not isinstance(item, dict):
                continue
            found.append(normalize_finding(item, source=path.stem))
    return found


def _recorded_review_results(work: Path) -> Optional[List[Dict[str, Any]]]:
    state_path = work / "state.json"
    if not state_path.is_file():
        return None
    try:
        data = load_json(state_path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    last = data.get("last_review")
    if not isinstance(last, dict):
        return None
    rows = as_list(last.get("results"))
    if not rows:
        return None
    out = []
    for item in rows:
        if isinstance(item, dict) and as_str(item.get("name")):
            out.append(item)
    return out or None


_GUARDIAN_LINK_KIND = {
    "r_to_a": "architecture",
    "a_to_t": "test",
    "t_to_i": "implementation",
    "i_to_r": "architecture",
    "invariant": "architecture",
}

_CHAIN_LABELS = (
    ("r_to_a", "R→A"),
    ("a_to_t", "A→T"),
    ("t_to_i", "T→I"),
    ("i_to_r", "I→R"),
)


def format_chain(chain: Any) -> str:
    """One log fragment: R→A ok  A→T gap  T→I ok  I→R fail."""
    if not isinstance(chain, dict):
        return "chain=(none)"
    parts = []
    for key, label in _CHAIN_LABELS:
        cell = chain.get(key)
        if not isinstance(cell, dict):
            parts.append("%s ?" % label)
            continue
        if cell.get("ok") is True:
            mark = "ok"
        elif cell.get("ok") is False:
            mark = "fail"
        else:
            mark = "?"
        parts.append("%s %s" % (label, mark))
    return "  ".join(parts)


def collect_guardian_findings(work: Path) -> List[Dict[str, Any]]:
    path = work / "prompts" / "guardian.result.json"
    if not path.is_file():
        return []
    try:
        data = load_json(path)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out: List[Dict[str, Any]] = []
    for item in as_list(data.get("risks")):
        if not isinstance(item, dict):
            continue
        row = normalize_finding(item, source="guardian")
        link = as_str(item.get("link")).strip().lower()
        row["kind"] = _GUARDIAN_LINK_KIND.get(link, "architecture")
        if row["severity"] == "?":
            row["severity"] = "invariant"
        if link:
            row["title"] = "[%s] %s" % (link, row["title"])
        out.append(row)
    return out


def collect_all(work: Path) -> List[Dict[str, Any]]:
    return collect_review_findings(work) + collect_guardian_findings(work)


def reviewer_result_present(work: Path) -> bool:
    prompts = work / "prompts"
    if not prompts.is_dir():
        return False
    return any(prompts.glob("reviewer-*.result.json"))


def kind_is_unclassified(value: Any) -> bool:
    return normalize_kind(value) == "unclassified"


def needs_classify(findings: Iterable[Dict[str, Any]], *, work: Optional[Path] = None) -> bool:
    rows = list(findings)
    if any(kind_is_unclassified(item.get("kind")) for item in rows):
        return True
    if work is not None and _unscoped_has_unclassified(work):
        return True
    if work is None:
        return False
    review_md = work / "review.md"
    if review_md.is_file() and not reviewer_result_present(work):
        return True
    return False


def _unscoped_has_unclassified(work: Path) -> bool:
    prompts = work / "prompts"
    if not prompts.is_dir():
        return False
    for path in prompts.glob("reviewer-*.result.json"):
        try:
            data = load_json(path)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for item in as_list(data.get("findings")):
            if isinstance(item, dict) and kind_is_unclassified(item.get("kind")):
                return True
    return False


def fill_missing_kinds(findings: Iterable[Dict[str, Any]], default: str = "unclassified") -> List[Dict[str, Any]]:
    out = []
    for item in findings:
        row = dict(item)
        row["kind"] = normalize_kind(row.get("kind")) or default
        out.append(row)
    return out


def group_by_kind(findings: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups = {kind: [] for kind in KINDS}
    for item in findings:
        kind = normalize_kind(item.get("kind")) or "unclassified"
        if kind not in groups:
            groups[kind] = []
        groups[kind].append(item)
    return groups


def actionable(findings: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item in findings if (item.get("kind") or "") in ACTIONABLE]


def write_findings(work: Path, findings: List[Dict[str, Any]]) -> Path:
    return dump_json(
        work / "findings.json",
        {
            "findings": findings,
            "counts": {kind: len(rows) for kind, rows in group_by_kind(findings).items()},
        },
    )


def importance_key(item: Dict[str, Any]) -> tuple:
    sev = (item.get("severity") or "?").strip().lower()
    kind = item.get("kind") or ""
    return (_SEVERITY_RANK.get(sev, 5), _KIND_RANK.get(kind, 4))


def take_important(items: Iterable[Dict[str, Any]], limit: int = CONSOLE_LIMIT) -> List[Dict[str, Any]]:
    rows = list(items)
    return sorted(rows, key=importance_key)[: max(0, limit)]


def format_console_lines(
    items: Iterable[Dict[str, Any]],
    *,
    limit: int = CONSOLE_LIMIT,
    sort: bool = False,
    more_hint: str = "",
) -> List[str]:
    """Plain CLI lines for a finished role. Empty if there is nothing to show."""
    rows = [item for item in items if item]
    if not rows:
        return []
    shown = take_important(rows, limit) if sort else rows[:limit]
    leftover = len(rows) - len(shown)
    lines: List[str] = []
    for i, item in enumerate(shown, 1):
        title = as_str(item.get("title")) or "(untitled)"
        sev = as_str(item.get("severity"))
        kind = as_str(item.get("kind"))
        path = as_str(item.get("path"))
        tags = "/".join(p for p in (sev, kind) if p and p != "?")
        loc = "  %s" % path if path else ""
        lines.append("  %d. [%s] %s%s" % (i, tags or "?", title, loc))
        ev = as_str(item.get("evidence")).strip().replace("\n", " ")
        if ev:
            if len(ev) > 140:
                ev = ev[:137] + "..."
            lines.append("      %s" % ev)
    if leftover > 0:
        lines.append("  +%d more in %s" % (leftover, more_hint or "followups.md"))
    return lines


def items_from_strings(values: Iterable[Any], *, severity: str = "", kind: str = "") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in values:
        if isinstance(raw, dict):
            out.append(normalize_finding(raw))
            continue
        title = as_str(raw).strip()
        if not title:
            continue
        out.append(
            {
                "severity": severity or "?",
                "title": title,
                "evidence": "",
                "path": "",
                "kind": kind,
                "source": "",
            }
        )
    return out


def render_followups(findings: Iterable[Dict[str, Any]]) -> str:
    rows = list(findings)
    lines = ["# Open classes", ""]
    if not rows:
        lines.append("- (none recorded in reviewer/guardian structured output)")
        return "\n".join(lines) + "\n"
    for item in rows:
        kind = item.get("kind") or "?"
        sev = item.get("severity") or "?"
        title = item.get("title") or "(untitled)"
        loc = item.get("path") or ""
        loc_s = " (`%s`)" % loc if loc else ""
        lines.append("- **%s** [%s] %s%s" % (sev, kind, title, loc_s))
    return "\n".join(lines) + "\n"


def render_plan(
    groups: Dict[str, List[Dict[str, Any]]],
    *,
    reclassified: bool = False,
) -> str:
    lines = [
        "# Apply plan",
        "",
        "Re-reviewed to classify: %s" % ("yes" if reclassified else "no"),
        "",
        "Order: design (architecture) → contract + tests → implementation → suite → review.",
        "",
    ]
    for kind in KINDS:
        rows = groups.get(kind) or []
        lines.append("## %s (%d)" % (kind, len(rows)))
        lines.append("")
        if not rows:
            lines.append("- (none)")
            lines.append("")
            continue
        for item in rows:
            loc = item.get("path") or ""
            loc_s = " (`%s`)" % loc if loc else ""
            lines.append(
                "- **%s** %s%s"
                % (item.get("severity") or "?", item.get("title") or "(untitled)", loc_s)
            )
        lines.append("")
    if not any(groups.get(k) for k in ACTIONABLE):
        lines.append("No actionable findings. Notes stay in followups.md.")
        lines.append("")
    return "\n".join(lines)


def render_summary(
    groups: Dict[str, List[Dict[str, Any]]],
    *,
    reclassified: bool,
    suite_status: str,
    hops: List[str],
    rereviewed: bool,
) -> str:
    lines = [
        "# Apply",
        "",
        "Re-reviewed to classify: %s" % ("yes" if reclassified else "no"),
        "Closing review: %s" % ("yes" if rereviewed else "no"),
        "Suite: %s" % (suite_status or "UNVERIFIED"),
        "",
        "Counts: architecture=%d implementation=%d test=%d note=%d unclassified=%d"
        % (
            len(groups.get("architecture") or []),
            len(groups.get("implementation") or []),
            len(groups.get("test") or []),
            len(groups.get("note") or []),
            len(groups.get("unclassified") or []),
        ),
        "",
        "## Hops",
        "",
    ]
    if hops:
        lines.extend("- %s" % hop for hop in hops)
    else:
        lines.append("- (none)")
    lines.append("")
    return "\n".join(lines)


def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[tuple, Dict[str, Any]] = {}
    extras: List[Dict[str, Any]] = []
    order: List[tuple] = []
    for item in items:
        title = (item.get("title") or "").strip().lower()
        path = (item.get("path") or "").strip()
        kind = item.get("kind") or ""
        if not title and not path:
            extras.append(item)
            continue
        evidence = (item.get("evidence") or "").strip()
        key = (path, title, evidence)
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = item
            order.append(key)
            continue
        prev_kind = prev.get("kind") or ""
        if not prev_kind and kind:
            by_key[key] = item
        elif kind and prev_kind and kind != prev_kind:
            extras.append(item)
    return [by_key[k] for k in order] + extras
