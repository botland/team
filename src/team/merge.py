from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple


def merge_reviews(parts: Sequence[Tuple[str, Dict[str, Any], str]]) -> str:
    """parts: (label, schema_output, markdown)."""
    lines = ["# Review", ""]
    if len(parts) >= 2:
        overlap = _overlap(parts)
        lines.append("## Overlap")
        lines.append("")
        if overlap:
            lines.extend("- %s" % item for item in overlap)
        else:
            lines.append("- (no shared path+title hits)")
        lines.append("")
    for label, data, markdown in parts:
        lines.append("## %s" % label)
        lines.append("")
        summary = (data or {}).get("summary") or ""
        if summary:
            lines.append(summary)
            lines.append("")
        findings = (data or {}).get("findings") or []
        if findings:
            lines.append("### Findings")
            lines.append("")
            for finding in findings:
                title = finding.get("title") or "(untitled)"
                sev = finding.get("severity") or "?"
                kind = finding.get("kind") or ""
                kind_s = " [%s]" % kind if kind else ""
                path = finding.get("path") or ""
                loc = " (`%s`)" % path if path else ""
                lines.append("- **%s**%s %s%s" % (sev, kind_s, title, loc))
                ev = finding.get("evidence") or ""
                if ev:
                    lines.append("  - %s" % ev)
            lines.append("")
        body = markdown.strip() if markdown else ""
        if body:
            lines.append(body)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _overlap(parts: Sequence[Tuple[str, Dict[str, Any], str]]) -> List[str]:
    if len(parts) < 2:
        return []
    bags: List[set] = []
    for _label, data, _md in parts:
        keys = set()
        for finding in (data or {}).get("findings") or []:
            title = (finding.get("title") or "").strip().lower()
            path = (finding.get("path") or "").strip()
            if title or path:
                keys.add((path, title))
        bags.append(keys)
    shared = bags[0]
    for bag in bags[1:]:
        shared = shared & bag
    out = []
    for path, title in sorted(shared):
        if path and title:
            out.append("`%s` — %s" % (path, title))
        else:
            out.append(title or path)
    return out
