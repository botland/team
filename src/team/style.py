from __future__ import annotations

import os
import re
import sys
from typing import Callable, Optional, TextIO

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
BRIGHT_RED = "\033[91m"
BRIGHT_GREEN = "\033[92m"
BRIGHT_YELLOW = "\033[93m"
BRIGHT_BLUE = "\033[94m"
BRIGHT_MAGENTA = "\033[95m"

_SEVERITY = {
    "critical": (BOLD, BRIGHT_RED),
    "high": (BOLD, RED),
    "invariant": (BOLD, MAGENTA),
    "medium": (YELLOW,),
    "low": (CYAN,),
    "issue": (YELLOW,),
}

_KIND = {
    "architecture": (BRIGHT_BLUE,),
    "implementation": (BRIGHT_GREEN,),
    "test": (BRIGHT_YELLOW,),
    "note": (DIM,),
    "unclassified": (BOLD, BRIGHT_RED),
}

_STATUS = {
    "failed": (BOLD, RED),
    "applied": (GREEN,),
    "skipped": (YELLOW,),
    "stale": (DIM, MAGENTA),
    "reopened": (BOLD, CYAN),
    "pending": (DIM,),
    "seq-failed": (BOLD, RED),
    "needs-classification": (BOLD, YELLOW),
    "seq-reopened": (CYAN,),
    "complete": (GREEN,),
}

_LINK = {
    "r_to_a": (CYAN,),
    "a_to_t": (BRIGHT_YELLOW,),
    "t_to_i": (BRIGHT_GREEN,),
    "i_to_r": (BRIGHT_MAGENTA,),
    "invariant": (BOLD, MAGENTA),
}

_LINK_TAG = re.compile(
    r"\[(r_to_a|a_to_t|t_to_i|i_to_r|invariant)\]",
    re.IGNORECASE,
)

_ANSI = re.compile(r"\033\[[0-9;]*m")

Painter = Callable[..., str]


def color_enabled(stream: Optional[TextIO] = None) -> bool:
    """NO_COLOR wins; FORCE_COLOR forces on; otherwise TTY on stream (stdout)."""
    if os.environ.get("NO_COLOR", ""):
        return False
    if os.environ.get("FORCE_COLOR", ""):
        return True
    target = stream if stream is not None else sys.stdout
    try:
        return bool(target.isatty())
    except Exception:
        return False


def paint(text: str, *codes: str, enabled: Optional[bool] = None) -> str:
    if text == "" or not codes:
        return text
    if enabled is None:
        enabled = color_enabled()
    if not enabled:
        return text
    return "%s%s%s" % ("".join(codes), text, RESET)


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text or "")


def dim(text: str, *, enabled: Optional[bool] = None) -> str:
    return paint(text, DIM, enabled=enabled)


def path(text: str, *, enabled: Optional[bool] = None) -> str:
    return paint(text, DIM, CYAN, enabled=enabled)


def tokens(text: str, *, enabled: Optional[bool] = None) -> str:
    return paint(text, CYAN, enabled=enabled)


def usd(text: str, *, complete: bool = True, enabled: Optional[bool] = None) -> str:
    """Known complete $ is green. Missing or partial $ is yellow, never quiet."""
    if not complete:
        return paint(text, YELLOW, enabled=enabled)
    return paint(text, BRIGHT_GREEN, enabled=enabled)


def severity(text: str, *, enabled: Optional[bool] = None) -> str:
    key = (text or "").strip().lower()
    return paint(text, *_SEVERITY.get(key, ()), enabled=enabled)


def kind(text: str, *, enabled: Optional[bool] = None) -> str:
    key = (text or "").strip().lower()
    return paint(text, *_KIND.get(key, ()), enabled=enabled)


def status(text: str, *, enabled: Optional[bool] = None) -> str:
    key = (text or "").strip().lower()
    return paint(text, *_STATUS.get(key, ()), enabled=enabled)


def link_codes(value: str) -> tuple:
    key = (value or "").strip().lower().strip("[]")
    return _LINK.get(key, ())


def link(text: str, *, enabled: Optional[bool] = None, key: Optional[str] = None) -> str:
    return paint(text, *link_codes(key if key is not None else text), enabled=enabled)


def link_tags(text: str, *, enabled: Optional[bool] = None) -> str:
    """Color guardian [r_to_a] / [t_to_i] / … tokens inside a title."""
    if not text:
        return text
    if enabled is None:
        enabled = color_enabled()
    if not enabled:
        return text

    def _one(match: re.Match) -> str:
        return link(match.group(0), enabled=True)

    return _LINK_TAG.sub(_one, text)


def ljust(text: str, width: int, painter: Painter, *, enabled: Optional[bool] = None) -> str:
    """Pad on visible width so ANSI codes do not shift columns."""
    raw = text or ""
    pad = max(0, width - len(raw))
    return painter(raw, enabled=enabled) + (" " * pad)


def tag_pair(
    sev: str,
    kind_name: str,
    *,
    enabled: Optional[bool] = None,
) -> str:
    bits = []
    if sev and sev != "?":
        bits.append(severity(sev, enabled=enabled))
    if kind_name and kind_name != "?":
        bits.append(kind(kind_name, enabled=enabled))
    return "/".join(bits) if bits else "?"
