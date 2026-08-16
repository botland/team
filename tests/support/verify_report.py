"""Parse ``git/verify-<phase>.md`` headings as whole lines.

``violations:`` and ``already_dirty (since run start):`` are sibling
headings. Path lines are ``  <path>`` until the next non-indented line.
"""

from __future__ import annotations

from typing import List


ALREADY_DIRTY_HEADING = "already_dirty (since run start):"
VIOLATIONS_HEADING = "violations:"


def heading_paths(text: str, heading: str) -> List[str]:
    found: List[str] = []
    in_sec = False
    for line in text.splitlines():
        if line == heading:
            in_sec = True
            continue
        if in_sec:
            if line.startswith("  "):
                rel = line.strip()
                if rel:
                    found.append(rel)
            else:
                break
    return found


def has_heading(text: str, heading: str) -> bool:
    return any(line == heading for line in text.splitlines())
