"""Provider spend: tokens and dollars from a headless hop envelope.

The CLI envelope is the only source. Missing cost is unknown, never $0.
Partial or incomplete spend omits every cost float so a sum cannot become
a fake complete bill. Do not invent a price table; do not sum modelUsage
rows into a hop total.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from team import style
from team.util import write_text


USAGE_JSONL = "usage.jsonl"
USAGE_MD = "usage.md"

# Grok documents 1 USD = 10^10 ticks. Sum ticks when every cost hop has them.
_TICKS_PER_USD = 10**10

_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_tokens",
    "total_tokens",
)

_TOKEN_ALIASES = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
    "reasoningTokens": "reasoning_tokens",
    "totalTokens": "total_tokens",
}

_WRITE_LOCK = threading.Lock()


@dataclass
class Usage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_input_tokens: Optional[int] = None
    cache_creation_input_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    cost_usd_ticks: Optional[int] = None
    cost_is_partial: bool = False
    usage_is_incomplete: bool = False
    models: Dict[str, Any] = field(default_factory=dict)

    def has_tokens(self) -> bool:
        return any(getattr(self, key) is not None for key in _TOKEN_KEYS)

    def has_cost(self) -> bool:
        """True only when the provider stamped a complete dollar figure."""
        if self.cost_is_partial or self.usage_is_incomplete:
            return False
        return self.cost_usd is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "cost_usd_ticks": self.cost_usd_ticks,
            "cost_is_partial": self.cost_is_partial,
            "usage_is_incomplete": self.usage_is_incomplete,
            "models": dict(self.models) if self.models else {},
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["Usage"]:
        if not isinstance(data, dict):
            return None
        return parse_usage(data)


def parse_usage(wrapper: Optional[Dict[str, Any]]) -> Optional[Usage]:
    """Read spend from a headless JSON envelope (any coding-agent adapter).

    Returns None when the object has no spend fields (prompt never reached
    the model, or this is not an envelope). Absence of ``total_cost_usd``
    is unknown, not free. ``cost_is_partial`` / ``usage_is_incomplete``
    drop every cost float even if a number is present.
    """
    if not isinstance(wrapper, dict):
        return None
    raw_usage = wrapper.get("usage")
    usage_obj = raw_usage if isinstance(raw_usage, dict) else {}
    models = wrapper.get("modelUsage")
    if not isinstance(models, dict):
        models = wrapper.get("model_usage")
    if not isinstance(models, dict):
        models = wrapper.get("models")
    if not isinstance(models, dict):
        models = {}
    incomplete = bool(wrapper.get("usage_is_incomplete"))
    partial = bool(wrapper.get("cost_is_partial"))
    has_marker = (
        bool(usage_obj)
        or any(wrapper.get(key) is not None for key in _TOKEN_KEYS)
        or "total_cost_usd" in wrapper
        or "total_cost_usd_ticks" in wrapper
        or "cost_usd" in wrapper
        or "cost_usd_ticks" in wrapper
        or incomplete
        or partial
        or bool(models)
    )
    if not has_marker:
        return None
    tokens: Dict[str, Optional[int]] = {key: None for key in _TOKEN_KEYS}
    for key in _TOKEN_KEYS:
        if key in usage_obj:
            tokens[key] = _as_int(usage_obj.get(key))
        elif key in wrapper and tokens[key] is None:
            tokens[key] = _as_int(wrapper.get(key))
    for alias, key in _TOKEN_ALIASES.items():
        if tokens[key] is None and alias in usage_obj:
            tokens[key] = _as_int(usage_obj.get(alias))
    if tokens["total_tokens"] is None:
        parts = [
            tokens["input_tokens"],
            tokens["output_tokens"],
            tokens["cache_read_input_tokens"],
            tokens["cache_creation_input_tokens"],
        ]
        if all(part is not None for part in parts):
            tokens["total_tokens"] = int(sum(parts))
    cost: Optional[float] = None
    ticks: Optional[int] = None
    if not incomplete and not partial:
        if "total_cost_usd" in wrapper:
            cost = _as_float(wrapper.get("total_cost_usd"))
        elif "cost_usd" in wrapper:
            cost = _as_float(wrapper.get("cost_usd"))
        if "total_cost_usd_ticks" in wrapper:
            ticks = _as_int(wrapper.get("total_cost_usd_ticks"))
        elif "cost_usd_ticks" in wrapper:
            ticks = _as_int(wrapper.get("cost_usd_ticks"))
        # Never derive hop cost from modelUsage rows. Partial bills omit
        # those floats for a reason; a complete envelope already set
        # total_cost_usd.
    return Usage(
        input_tokens=tokens["input_tokens"],
        output_tokens=tokens["output_tokens"],
        cache_read_input_tokens=tokens["cache_read_input_tokens"],
        cache_creation_input_tokens=tokens["cache_creation_input_tokens"],
        reasoning_tokens=tokens["reasoning_tokens"],
        total_tokens=tokens["total_tokens"],
        cost_usd=cost,
        cost_usd_ticks=ticks,
        cost_is_partial=partial,
        usage_is_incomplete=incomplete,
        models=dict(models),
    )


def hop_record(
    *,
    slug: str,
    phase: str,
    role: str,
    runtime: str,
    session_id: str,
    success: bool,
    num_turns: Optional[int],
    usage: Optional[Usage],
    ts: Optional[str] = None,
) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "ts": ts or _now_utc(),
        "slug": slug,
        "phase": phase,
        "role": role,
        "runtime": runtime,
        "session_id": session_id or "",
        "success": bool(success),
        "num_turns": num_turns,
    }
    if usage is not None:
        rec.update(usage.to_dict())
    return rec


def repo_ledger_path(work_or_repo: Path) -> Optional[Path]:
    """Durable ledger: ``<repo>/.team/work/usage.jsonl``.

    Lives next to slug dirs, so ``--force`` on a slug cannot erase spend.
    ``work_or_repo`` may be the slug work dir or the target repo root.
    """
    path = Path(work_or_repo)
    if path.name == "work" and path.parent.name == ".team":
        return path / USAGE_JSONL
    if (path / ".team" / "work").is_dir() or (path / ".team").exists():
        return path / ".team" / "work" / USAGE_JSONL
    if path.parent.name == "work" and path.parent.parent.name == ".team":
        return path.parent / USAGE_JSONL
    return None


def record_hop(work: Path, record: Dict[str, Any]) -> Path:
    """Append one hop to the slug log and the durable repo ledger."""
    jsonl = work / USAGE_JSONL
    md = work / USAGE_MD
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    with _WRITE_LOCK:
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(line)
        ledger = repo_ledger_path(work)
        if ledger is not None and ledger.resolve() != jsonl.resolve():
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with ledger.open("a", encoding="utf-8") as fh:
                fh.write(line)
        hops = _read_jsonl(jsonl)
        write_text(md, render_markdown(hops, slug=str(record.get("slug") or "")))
    return md


def load_hops(work: Path) -> List[Dict[str, Any]]:
    path = work / USAGE_JSONL
    if not path.is_file():
        return []
    return _read_jsonl(path)


def load_repo_hops(repo: Path, *, slug: str = "") -> List[Dict[str, Any]]:
    """Spend that survives ``--force``. Optionally filter to one slug."""
    path = repo_ledger_path(repo)
    if path is None or not path.is_file():
        if slug:
            return load_hops(repo / ".team" / "work" / slug)
        return []
    hops = _read_jsonl(path)
    if slug:
        hops = [row for row in hops if str(row.get("slug") or "") == slug]
        if not hops:
            return load_hops(repo / ".team" / "work" / slug)
    return hops


def hop_has_tokens(hop: Dict[str, Any]) -> bool:
    return any(hop.get(key) is not None for key in _TOKEN_KEYS)


def hop_has_cost(hop: Dict[str, Any]) -> bool:
    if hop.get("cost_is_partial") or hop.get("usage_is_incomplete"):
        return False
    return hop.get("cost_usd") is not None


def hop_total_tokens(hop: Dict[str, Any]) -> Optional[int]:
    total = _as_int(hop.get("total_tokens"))
    if total is not None:
        return total
    parts = [
        _as_int(hop.get("input_tokens")),
        _as_int(hop.get("output_tokens")),
        _as_int(hop.get("cache_read_input_tokens")),
        _as_int(hop.get("cache_creation_input_tokens")),
    ]
    if all(part is None for part in parts):
        return None
    return int(sum(part or 0 for part in parts))


def summarize(hops: List[Dict[str, Any]]) -> Dict[str, Any]:
    totals = {key: 0 for key in _TOKEN_KEYS}
    hops_with_tokens = 0
    hops_with_cost = 0
    hops_missing_cost = 0
    cost_sum = 0.0
    ticks_sum = 0
    ticks_complete = True
    for hop in hops:
        if hop_has_tokens(hop):
            hops_with_tokens += 1
            for key in _TOKEN_KEYS:
                value = _as_int(hop.get(key))
                if value is not None:
                    totals[key] += value
            if hop.get("total_tokens") is None:
                computed = hop_total_tokens(hop)
                if computed is not None:
                    totals["total_tokens"] += computed
            if hop_has_cost(hop):
                hops_with_cost += 1
                cost_sum += float(hop["cost_usd"])
                ticks = _as_int(hop.get("cost_usd_ticks"))
                if ticks is None:
                    ticks_complete = False
                else:
                    ticks_sum += ticks
            else:
                hops_missing_cost += 1
                ticks_complete = False
    if hops_with_cost == 0:
        cost_usd: Optional[float] = None
    elif ticks_complete:
        cost_usd = ticks_sum / float(_TICKS_PER_USD)
    else:
        cost_usd = cost_sum
    return {
        "hops": len(hops),
        "hops_with_tokens": hops_with_tokens,
        "hops_with_cost": hops_with_cost,
        "hops_missing_cost": hops_missing_cost,
        "cost_complete": hops_with_tokens > 0 and hops_missing_cost == 0,
        "cost_usd": cost_usd,
        **totals,
    }


def format_usd(
    value: Optional[float],
    *,
    complete: bool = True,
    missing: int = 0,
) -> str:
    if value is None:
        return "$ unknown"
    if 0 < abs(value) < 0.01:
        text = "$%.4f" % value
    else:
        text = "$%.2f" % value
    if complete:
        return text
    if missing:
        noun = "hop" if missing == 1 else "hops"
        return "%s known (%d %s omitted $)" % (text, missing, noun)
    return "%s known" % text


def compact_int(value: int) -> str:
    n = int(value)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n < 1000:
        return sign + str(n)
    if n < 1_000_000:
        scaled = n / 1000.0
        if abs(scaled - round(scaled)) < 0.05:
            return "%s%dk" % (sign, int(round(scaled)))
        return "%s%.1fk" % (sign, scaled)
    scaled = n / 1_000_000.0
    if abs(scaled - round(scaled)) < 0.005:
        return "%s%dM" % (sign, int(round(scaled)))
    return "%s%.2fM" % (sign, scaled)


def format_token_clause(hop: Dict[str, Any]) -> str:
    parts = []
    inn = _as_int(hop.get("input_tokens"))
    out = _as_int(hop.get("output_tokens"))
    cache = _as_int(hop.get("cache_read_input_tokens"))
    if inn is not None:
        parts.append("%s in" % compact_int(inn))
    if out is not None:
        parts.append("%s out" % compact_int(out))
    if cache:
        parts.append("%s cache" % compact_int(cache))
    if parts:
        return " / ".join(parts)
    if hop.get("usage_is_incomplete"):
        return "tokens incomplete"
    if hop_has_tokens(hop):
        total = hop_total_tokens(hop)
        if total is not None:
            return "%s tokens" % compact_int(total)
    return "spend omitted"


def format_hop_console(record: Dict[str, Any], *, enabled: Optional[bool] = None) -> str:
    if enabled is None:
        enabled = style.color_enabled()
    phase = str(record.get("phase") or "")
    runtime = str(record.get("runtime") or "")
    bits = [style.dim("usage", enabled=enabled)]
    if phase:
        bits.append(style.paint(phase, style.CYAN, enabled=enabled))
    if runtime:
        bits.append(style.dim("(%s)" % runtime, enabled=enabled))
    token_bit = format_token_clause(record)
    if token_bit in ("tokens incomplete", "spend omitted"):
        bits.append(style.dim(token_bit, enabled=enabled))
    else:
        bits.append(style.tokens(token_bit, enabled=enabled))
    has_cost = hop_has_cost(record)
    bits.append(
        style.usd(
            format_usd(record.get("cost_usd") if has_cost else None),
            complete=has_cost,
            enabled=enabled,
        )
    )
    turns = record.get("num_turns")
    if turns is not None:
        bits.append(
            style.dim(
                "%s turn%s" % (turns, "" if turns == 1 else "s"),
                enabled=enabled,
            )
        )
    return "  ".join(p for p in bits if p)


def format_summary_line(
    summary: Dict[str, Any], *, enabled: Optional[bool] = None, scope: str = ""
) -> str:
    """``scope`` names what the total covers when it is not just this run.

    The repo ledger is append-only and every range review reuses one default
    slug, so a slug-filtered ledger total is a lifetime figure. Printing it in
    the same shape as the per-run line is how a $11 run reads as $19.
    """
    if enabled is None:
        enabled = style.color_enabled()
    hops = int(summary.get("hops") or 0)
    noun = "hop" if hops == 1 else "hops"
    tokens = _as_int(summary.get("total_tokens"))
    token_bit = "%s tokens" % compact_int(tokens) if tokens else "tokens omitted"
    complete = bool(summary.get("cost_complete"))
    cost = format_usd(
        summary.get("cost_usd"),
        complete=complete,
        missing=int(summary.get("hops_missing_cost") or 0),
    )
    painted_tokens = (
        style.tokens(token_bit, enabled=enabled)
        if tokens
        else style.dim(token_bit, enabled=enabled)
    )
    return "%s  %d %s%s  %s  %s" % (
        style.dim("usage", enabled=enabled),
        hops,
        noun,
        style.dim(" (%s)" % scope, enabled=enabled) if scope else "",
        painted_tokens,
        style.usd(cost, complete=complete, enabled=enabled),
    )


def render_console(
    hops: List[Dict[str, Any]], *, slug: str = "", enabled: Optional[bool] = None
) -> str:
    if enabled is None:
        enabled = style.color_enabled()
    lines: List[str] = []
    if slug:
        lines.append("%s %s" % (style.dim("slug:", enabled=enabled), slug))
        lines.append("")
    for hop in hops:
        lines.append(format_hop_console(hop, enabled=enabled))
    lines.append(format_summary_line(summarize(hops), enabled=enabled))
    return "\n".join(lines)


def format_costs_listing(
    rows: List[tuple], *, enabled: Optional[bool] = None
) -> str:
    """rows is ``(slug, summary)``. ``total`` is painted as a summary row."""
    if enabled is None:
        enabled = style.color_enabled()
    header = "%-28s %5s %10s  %s" % ("SLUG", "HOPS", "TOKENS", "COST")
    lines = [style.dim(header, enabled=enabled)]
    for name, summary in rows:
        tokens = (
            summary.get("total_tokens") if summary.get("hops_with_tokens") else None
        )
        token_raw = compact_int(int(tokens)).rjust(10) if tokens else "-".rjust(10)
        complete = bool(summary.get("cost_complete"))
        cost = format_usd(
            summary.get("cost_usd"),
            complete=complete,
            missing=int(summary.get("hops_missing_cost") or 0),
        )
        label = str(name or "")
        if label == "total":
            slug_cell = style.paint(label, style.BOLD, enabled=enabled) + (
                " " * max(0, 28 - len(label))
            )
        else:
            slug_cell = label.ljust(28)
        token_cell = (
            style.tokens(token_raw, enabled=enabled)
            if tokens
            else style.dim(token_raw, enabled=enabled)
        )
        lines.append(
            "%s %5d %s  %s"
            % (
                slug_cell,
                summary.get("hops") or 0,
                token_cell,
                style.usd(cost, complete=complete, enabled=enabled),
            )
        )
    return "\n".join(lines)


def render_markdown(hops: List[Dict[str, Any]], *, slug: str = "") -> str:
    lines = ["# Usage", ""]
    if slug:
        lines.append("slug: %s" % slug)
        lines.append("")
    lines.append("| phase | runtime | turns | in | out | cache read | $ |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for hop in hops:
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s |"
            % (
                _md_cell(hop.get("phase")),
                _md_cell(hop.get("runtime")),
                _md_cell(hop.get("num_turns")),
                _md_num(hop.get("input_tokens")),
                _md_num(hop.get("output_tokens")),
                _md_num(hop.get("cache_read_input_tokens")),
                _md_cost(hop),
            )
        )
    lines.append("")
    summary = summarize(hops)
    lines.append(format_summary_line(summary, enabled=False) + ".")
    if summary.get("hops_with_tokens") and not summary.get("cost_complete"):
        lines.append(
            "Missing $ is unreported or incomplete, never free. "
            "Do not treat the known subtotal as the bill."
        )
    lines.append("")
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return str(value).replace("|", "\\|")


def _md_num(value: Any) -> str:
    parsed = _as_int(value)
    return str(parsed) if parsed is not None else "—"


def _md_cost(hop: Dict[str, Any]) -> str:
    if not hop_has_cost(hop):
        return "—"
    value = hop.get("cost_usd")
    if isinstance(value, float) and 0 < abs(value) < 0.01:
        return "%.4f" % value
    try:
        return "%.4f" % float(value)
    except (TypeError, ValueError):
        return "—"


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
