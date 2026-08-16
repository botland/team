from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import hashlib

from team import style
from team.util import as_list, as_str, dump_json, load_json, normalize_root


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

# apply --seq follows the feature rail, not console urgency.
_SEQ_KIND_RANK = {
    "architecture": 0,
    "test": 1,
    "implementation": 2,
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
        except Exception as exc:
            raise FindingsError(
                "unreadable review result %s: %s" % (path.name, exc)
            ) from exc
        if not isinstance(data, dict):
            raise FindingsError("review result is not an object: %s" % path.name)
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


def unrecorded_reviewer_results(work: Path, recorded: List[Dict[str, Any]]) -> List[str]:
    names = {as_str(row.get("name")) for row in recorded if as_str(row.get("name"))}
    prompts = work / "prompts"
    if not prompts.is_dir():
        return []
    extras = []
    for path in sorted(prompts.glob("reviewer-*.result.json")):
        if path.name not in names:
            extras.append(path.name)
    return extras


def recorded_inspect_unfinished(work: Path) -> bool:
    """True when a pinned result is a progress note or recorded short turns."""
    from team.runners import inspect_progress_note

    try:
        recorded = _recorded_review_results(work)
    except FindingsError:
        return False
    if not recorded:
        return False
    prompts = work / "prompts"
    for row in recorded:
        turns = row.get("num_turns")
        if turns is not None:
            try:
                if int(turns) <= 1:
                    return True
            except (TypeError, ValueError):
                return True
        name = as_str(row.get("name"))
        path = prompts / name if name else None
        if path is None or not path.is_file():
            continue
        try:
            data = load_json(path)
        except Exception:
            continue
        if isinstance(data, dict) and inspect_progress_note(data):
            return True
    return False


# Closed link enum (same set as the guardian persona). Valid link is the only
# kind authority; missing/empty/unknown is unclassified, never architecture.
# I→R is implementation vs brief — it does not take the replan rail.
_GUARDIAN_LINK_KIND = {
    "r_to_a": "architecture",
    "a_to_t": "test",
    "t_to_i": "implementation",
    "i_to_r": "implementation",
    "invariant": "architecture",
}

_CHAIN_LABELS = (
    ("r_to_a", "R→A"),
    ("a_to_t", "A→T"),
    ("t_to_i", "T→I"),
    ("i_to_r", "I→R"),
)

_CHAIN_KEYS = tuple(key for key, _label in _CHAIN_LABELS)


def _normalize_guardian_link(value: Any) -> str:
    return as_str(value).strip().casefold()


def _guardian_kind_for_link(link: str) -> str:
    return _GUARDIAN_LINK_KIND.get(link, "unclassified")


def format_chain(chain: Any, *, color: Optional[bool] = None) -> str:
    """One log fragment: R→A ok  A→T gap  T→I ok  I→R fail."""
    if not isinstance(chain, dict):
        return "chain=(none)"
    if color is None:
        color = False
    parts = []
    for key, label in _CHAIN_LABELS:
        cell = chain.get(key)
        colored_label = style.link(label, key=key, enabled=color)
        if not isinstance(cell, dict):
            parts.append("%s ?" % colored_label)
            continue
        if cell.get("ok") is True:
            mark = style.paint("ok", style.GREEN, enabled=color)
        elif cell.get("ok") is False:
            mark = style.paint("fail", style.BOLD, style.RED, enabled=color)
        else:
            mark = "?"
        parts.append("%s %s" % (colored_label, mark))
    return "  ".join(parts)


def collect_guardian_findings(work: Path) -> List[Dict[str, Any]]:
    path = work / "prompts" / "guardian.result.json"
    if not path.is_file():
        return []
    try:
        data = load_json(path)
    except Exception as exc:
        raise FindingsError(
            "unreadable guardian result %s: %s" % (path.name, exc)
        ) from exc
    if not isinstance(data, dict):
        raise FindingsError("guardian result is not an object: %s" % path.name)
    from team.runners import inspect_progress_note

    # A progress note is not a review. Explicit risks on any other artifact
    # are apply work — missing turn metadata must not drop them.
    if inspect_progress_note(data):
        return []
    chain = data.get("chain")
    if _chain_has_unjudged(chain):
        return []
    out: List[Dict[str, Any]] = []
    covered: set = set()
    for item in as_list(data.get("risks")):
        if not isinstance(item, dict):
            continue
        row = normalize_finding(item, source="guardian")
        link = _normalize_guardian_link(item.get("link"))
        # Link is the only kind authority; explicit risk.kind is overwritten.
        row["kind"] = _guardian_kind_for_link(link)
        if row["severity"] == "?":
            row["severity"] = "invariant"
        if link:
            row["title"] = "[%s] %s" % (link, row["title"])
        if link in _GUARDIAN_LINK_KIND and link != "invariant":
            covered.add(link)
        out.append(row)
    # Converse of "every risk has a link": every failed chain cell has a row.
    # invariant is one architecture claim; it does not cover other cells.
    # Synthetics need finished-inspect evidence so an empty-risk inspecting
    # payload does not become four queue rows.
    if isinstance(chain, dict) and not _guardian_artifact_unfinished(data):
        for key in _CHAIN_KEYS:
            cell = chain.get(key)
            if not isinstance(cell, dict) or cell.get("ok") is not False:
                continue
            if key in covered:
                continue
            note = as_str(cell.get("note"))
            out.append(
                {
                    "severity": "invariant",
                    "title": "[%s] failed chain cell" % key,
                    "evidence": note,
                    "path": "",
                    "kind": _GUARDIAN_LINK_KIND[key],
                    "source": "guardian",
                }
            )
    return out


def _guardian_artifact_unfinished(data: Dict[str, Any]) -> bool:
    from team.runners import inspect_turn_count, unfinished_inspect

    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    role = as_str(meta.get("role")) or "guardian"
    return unfinished_inspect(
        role=role,
        num_turns=inspect_turn_count(data),
        output=data,
    )


def _chain_cell_judged(cell: Any) -> bool:
    """True when the cell is a finished judgment.

    Boolean ``ok`` is judged. ``ok=null`` with a non-empty note is n/a
    (missing layer on range/audit), not unjudged. Missing ``ok``, an
    empty note on null, or a non-dict cell drops the whole artifact.
    """
    if not isinstance(cell, dict) or "ok" not in cell:
        return False
    ok = cell.get("ok")
    if isinstance(ok, bool):
        return True
    if ok is None and as_str(cell.get("note")).strip():
        return True
    return False


def _chain_has_unjudged(chain: Any) -> bool:
    """True when a present cell was never judged (malformed or empty n/a)."""
    if not isinstance(chain, dict):
        return False
    for key in _CHAIN_KEYS:
        if key not in chain:
            continue
        if not _chain_cell_judged(chain.get(key)):
            return True
    return False


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
    """Classify from the collected/pinned findings, not leftover extras.

    ``findings`` is the attempt (pin when recorded). An unclassified extra
    ``reviewer-*.result.json`` is not authority. Markdown-only review.md
    with no result files still needs classify.
    """
    rows = list(findings)
    if any(kind_is_unclassified(item.get("kind")) for item in rows):
        return True
    if work is None:
        return False
    review_md = work / "review.md"
    if review_md.is_file() and not reviewer_result_present(work):
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


def finding_path(item: Dict[str, Any]) -> str:
    """One path encoding for collect, seq-id, and related_guardian."""
    return normalize_root(as_str(item.get("path")).strip())


def finding_identity(item: Dict[str, Any]) -> tuple:
    """Collect, seq-id, and related agree on this key. There is one opinion.

    Normalized path, title, and evidence -- what the defect *is*. Two rows
    that differ only in evidence are two classes; ``./src/a.py`` and
    ``src/a.py`` are one path.

    ``kind`` is deliberately not part of it. Kind is the router's answer
    (architecture -> replan, test -> tdd, implementation -> implementer) and
    a re-review can change it for the same defect; with kind in the key,
    re-classifying a finding minted a new class id, so the seq ledger's
    ``applied`` row stopped matching and apply ran the class a second time.

    What remains representable: the title is model text, so a re-worded
    finding is still a new class. Nothing in the payload is more stable, and
    the ledger no longer strands on it (resolve_seq_state).
    """
    return (
        finding_path(item),
        as_str(item.get("title")).strip().lower(),
        as_str(item.get("evidence")).strip(),
    )


def finding_id(item: Dict[str, Any]) -> str:
    raw = "%s|%s|%s" % finding_identity(item)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def is_review_finding(item: Dict[str, Any]) -> bool:
    return as_str(item.get("source")).startswith("reviewer")


def empty_seq_state() -> Dict[str, Any]:
    return {
        "applied": [],
        "skipped": [],
        "stale": [],
        "failed": "",
        "resume": "",
        "steps": [],
    }


def load_seq_state(work: Path) -> Dict[str, Any]:
    path = work / "findings.json"
    if not path.is_file():
        return empty_seq_state()
    try:
        data = load_json(path)
    except Exception:
        return empty_seq_state()
    if not isinstance(data, dict) or not isinstance(data.get("seq"), dict):
        return empty_seq_state()
    seq = data["seq"]
    return {
        "applied": [as_str(x) for x in as_list(seq.get("applied")) if as_str(x)],
        "skipped": [as_str(x) for x in as_list(seq.get("skipped")) if as_str(x)],
        "stale": [as_str(x) for x in as_list(seq.get("stale")) if as_str(x)],
        "failed": as_str(seq.get("failed")),
        "resume": as_str(seq.get("resume")),
        "steps": [row for row in as_list(seq.get("steps")) if isinstance(row, dict)],
    }


def write_findings(
    work: Path,
    findings: List[Dict[str, Any]],
    *,
    seq: Optional[Dict[str, Any]] = None,
) -> Path:
    if seq is None:
        seq = load_seq_state(work)
    return dump_json(
        work / "findings.json",
        {
            "findings": findings,
            "counts": {kind: len(rows) for kind, rows in group_by_kind(findings).items()},
            "seq": seq,
        },
    )


def live_class_ids(findings: Iterable[Dict[str, Any]]) -> set:
    """Class ids the current pool still contains. One derivation of membership."""
    return {
        finding_id(item)
        for item in findings
        if (item.get("kind") or "") in ACTIONABLE
    }


def resolve_seq_state(
    seq: Dict[str, Any],
    findings: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Pending markers resolved against the live pool. Nothing else reads them raw.

    A class id the pool no longer holds is not pending. ``--reopen A``
    followed by a review that re-words the same defect gives it a new id
    (the title is part of the class id), and the old ``resume`` then had no
    exit: ``--skip-failed`` only clears ``failed``, and ``mark_seq_step``
    clears ``resume`` only when the class being marked *is* the resumed one.
    ``seq_apply_complete`` read the leftover as an unfinished queue forever,
    so every later ``apply --seq`` logged "queue not exhausted" and exited 0.

    One rule over the whole ``{resume, failed, stale}`` space, not a branch
    per way a marker can go missing. ``applied`` / ``skipped`` are history,
    not pending, and are left alone.
    """
    live = live_class_ids(findings)
    out = dict(seq)
    out["failed"] = as_str(seq.get("failed")) if as_str(seq.get("failed")) in live else ""
    out["resume"] = as_str(seq.get("resume")) if as_str(seq.get("resume")) in live else ""
    out["stale"] = [
        as_str(x) for x in as_list(seq.get("stale")) if as_str(x) and as_str(x) in live
    ]
    return out


def dropped_seq_markers(seq: Dict[str, Any], resolved: Dict[str, Any]) -> List[str]:
    """Class ids resolve_seq_state took out of the pending sets, in ledger order."""
    out: List[str] = []
    for key in ("failed", "resume"):
        was = as_str(seq.get(key))
        if was and was != as_str(resolved.get(key)):
            out.append(was)
    kept = {as_str(x) for x in as_list(resolved.get("stale"))}
    for x in as_list(seq.get("stale")):
        fid = as_str(x)
        if fid and fid not in kept and fid not in out:
            out.append(fid)
    return out


def seq_candidates(
    findings: Iterable[Dict[str, Any]],
    seq: Dict[str, Any],
) -> List[Dict[str, Any]]:
    done = (
        set(seq.get("applied") or [])
        | set(seq.get("skipped") or [])
        | set(seq.get("stale") or [])
    )
    out: List[Dict[str, Any]] = []
    for item in findings:
        if (item.get("kind") or "") not in ACTIONABLE:
            continue
        row = dict(item)
        row["id"] = finding_id(row)
        if row["id"] in done:
            continue
        out.append(row)
    return out


def seq_apply_complete(
    findings: Iterable[Dict[str, Any]],
    seq: Dict[str, Any],
) -> bool:
    """Whether --seq may set stop_reason=applied.

    Orthogonal to pick_next is None: stale is done only while resume or
    failed marks an unresolved prefix. Leftover stale with both empty is
    a dead-letter suffix, not a finished queue -- and a marker whose class
    left the pool is neither (resolve_seq_state).
    """
    findings = list(findings)
    actionable = live_class_ids(findings)
    seq = resolve_seq_state(seq, findings)
    applied = {as_str(x) for x in as_list(seq.get("applied")) if as_str(x)}
    skipped = {as_str(x) for x in as_list(seq.get("skipped")) if as_str(x)}
    stale = {as_str(x) for x in as_list(seq.get("stale")) if as_str(x)}
    leftover = actionable - applied - skipped
    unresolved = bool(as_str(seq.get("resume")) or as_str(seq.get("failed")))
    if unresolved:
        return False
    if stale:
        return False
    return not leftover


def pick_next_seq(
    findings: Iterable[Dict[str, Any]],
    seq: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    findings = list(findings)
    seq = resolve_seq_state(seq, findings)
    candidates = seq_candidates(findings, seq)
    for key in (as_str(seq.get("failed")), as_str(seq.get("resume"))):
        if not key:
            continue
        for item in candidates:
            if item.get("id") == key:
                return item
    ranked = sorted(candidates, key=seq_key)
    return ranked[0] if ranked else None


def related_guardian(
    item: Dict[str, Any],
    guardian: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    path = finding_path(item)
    if not path:
        return []
    return [row for row in guardian if finding_path(row) == path]


def mark_seq_step(
    seq: Dict[str, Any],
    item: Dict[str, Any],
    *,
    status: str,
    hops: Optional[List[str]] = None,
    suite: str = "",
) -> Dict[str, Any]:
    fid = as_str(item.get("id")) or finding_id(item)
    applied = [x for x in as_list(seq.get("applied")) if as_str(x) and as_str(x) != fid]
    skipped = [x for x in as_list(seq.get("skipped")) if as_str(x) and as_str(x) != fid]
    stale = [x for x in as_list(seq.get("stale")) if as_str(x) and as_str(x) != fid]
    failed = as_str(seq.get("failed"))
    resume = as_str(seq.get("resume"))
    if failed == fid:
        failed = ""
    if resume == fid and status != "reopened":
        resume = ""
    if status == "applied":
        applied.append(fid)
        # Prefix applied|skipped restores the suffix of that reopen.
        stale = []
    elif status == "skipped":
        skipped.append(fid)
        stale = []
    elif status == "failed":
        failed = fid
    elif status == "stale":
        stale.append(fid)
    elif status == "reopened":
        resume = fid
        failed = ""
    step = {
        "id": fid,
        "status": status,
        "kind": as_str(item.get("kind")),
        "title": as_str(item.get("title")) or "(untitled)",
        "path": as_str(item.get("path")),
        "hops": list(hops or []),
        "suite": suite,
    }
    steps = [row for row in as_list(seq.get("steps")) if isinstance(row, dict)]
    steps.append(step)
    return {
        "applied": applied,
        "skipped": skipped,
        "stale": stale,
        "failed": failed,
        "resume": resume,
        "steps": steps,
    }


def seq_status_for(item: Dict[str, Any], seq: Dict[str, Any]) -> str:
    fid = as_str(item.get("id")) or finding_id(item)
    if fid in set(seq.get("stale") or []):
        return "stale"
    if fid in set(seq.get("applied") or []):
        return "applied"
    if fid in set(seq.get("skipped") or []):
        return "skipped"
    if fid and fid == as_str(seq.get("failed")):
        return "failed"
    if fid and fid == as_str(seq.get("resume")):
        return "reopened"
    return ""


def seq_known_ids(seq: Dict[str, Any]) -> List[str]:
    seen = []
    for step in as_list(seq.get("steps")):
        if not isinstance(step, dict):
            continue
        fid = as_str(step.get("id"))
        if fid and fid not in seen:
            seen.append(fid)
    return seen


def seq_item_from_log(seq: Dict[str, Any], fid: str) -> Dict[str, Any]:
    item = {"id": fid, "kind": "", "title": "(untitled)", "path": ""}
    for step in as_list(seq.get("steps")):
        if not isinstance(step, dict):
            continue
        if as_str(step.get("id")) != fid:
            continue
        item = {
            "id": fid,
            "kind": as_str(step.get("kind")),
            "title": as_str(step.get("title")) or "(untitled)",
            "path": as_str(step.get("path")),
        }
    return item


def latest_seq_rows(seq: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One row per class id; status is current set membership (pending if none)."""
    rows: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for step in as_list(seq.get("steps")):
        if not isinstance(step, dict):
            continue
        fid = as_str(step.get("id"))
        if not fid:
            continue
        if fid not in rows:
            order.append(fid)
        rows[fid] = {
            "id": fid,
            "status": as_str(step.get("status")) or "?",
            "kind": as_str(step.get("kind")),
            "title": as_str(step.get("title")) or "(untitled)",
            "path": as_str(step.get("path")),
        }
    out = []
    for fid in order:
        row = dict(rows[fid])
        # Displayed status is set membership, not last log (stale log ≠ still stale).
        row["status"] = seq_status_for(row, seq)
        out.append(row)
    return out


def reopen_prefix(seq: Dict[str, Any], fid: str) -> Dict[str, Any]:
    """Open fid again and mark every later class stale."""
    fid = as_str(fid)
    if not fid:
        raise FindingsError("reopen needs a class id")
    steps = [row for row in as_list(seq.get("steps")) if isinstance(row, dict)]
    last_idx = None
    for i, step in enumerate(steps):
        if as_str(step.get("id")) == fid:
            last_idx = i
    if last_idx is None:
        raise FindingsError("no seq class %s" % fid)
    current = seq_status_for({"id": fid}, seq) or as_str(steps[last_idx].get("status"))
    if current not in ("applied", "failed"):
        raise FindingsError(
            "can reopen an applied or failed class, not %s (%s)" % (fid, current or "open")
        )
    later: List[str] = []
    seen = set()
    for step in steps[last_idx + 1 :]:
        sid = as_str(step.get("id"))
        if not sid or sid == fid or sid in seen:
            continue
        seen.add(sid)
        later.append(sid)
    out = dict(seq)
    for sid in later:
        out = mark_seq_step(out, seq_item_from_log(out, sid), status="stale")
    out = mark_seq_step(out, seq_item_from_log(out, fid), status="reopened")
    return out


def extract_assumptions(markdown: str) -> List[str]:
    keep = ("changed assumptions", "new acceptance criteria", "structural changes")
    lines: List[str] = []
    current = False
    for raw in (markdown or "").splitlines():
        if raw.startswith("#"):
            current = raw.lstrip("#").strip().lower() in keep
            continue
        if not current:
            continue
        text = raw.strip()
        if text.startswith("-"):
            text = text[1:].strip()
        if not text or text.lower() in ("none", "- none"):
            continue
        lines.append(text)
    return lines


def importance_key(item: Dict[str, Any]) -> tuple:
    sev = (item.get("severity") or "?").strip().lower()
    kind = item.get("kind") or ""
    return (_SEVERITY_RANK.get(sev, 5), _KIND_RANK.get(kind, 4))


def seq_key(item: Dict[str, Any]) -> tuple:
    """Feature rail: architecture → test → implementation, then severity."""
    sev = (item.get("severity") or "?").strip().lower()
    kind = item.get("kind") or ""
    return (_SEQ_KIND_RANK.get(kind, 4), _SEVERITY_RANK.get(sev, 5))


def take_important(items: Iterable[Dict[str, Any]], limit: int = CONSOLE_LIMIT) -> List[Dict[str, Any]]:
    rows = list(items)
    return sorted(rows, key=importance_key)[: max(0, limit)]


def format_console_lines(
    items: Iterable[Dict[str, Any]],
    *,
    limit: int = CONSOLE_LIMIT,
    sort: bool = False,
    more_hint: str = "",
    color: Optional[bool] = None,
) -> List[str]:
    """CLI lines for a finished role. Empty if there is nothing to show."""
    rows = [item for item in items if item]
    if not rows:
        return []
    if color is None:
        color = style.color_enabled()
    shown = take_important(rows, limit) if sort else rows[:limit]
    leftover = len(rows) - len(shown)
    lines: List[str] = []
    for i, item in enumerate(shown, 1):
        title = style.link_tags(as_str(item.get("title")) or "(untitled)", enabled=color)
        sev = as_str(item.get("severity"))
        kind_name = as_str(item.get("kind"))
        path = as_str(item.get("path"))
        tags = style.tag_pair(sev, kind_name, enabled=color)
        loc = "  %s" % style.path(path, enabled=color) if path else ""
        lines.append("  %d. [%s] %s%s" % (i, tags, title, loc))
        ev = as_str(item.get("evidence")).strip().replace("\n", " ")
        if ev:
            if len(ev) > 140:
                ev = ev[:137] + "..."
            lines.append("      %s" % style.dim(ev, enabled=color))
    if leftover > 0:
        lines.append(
            "  %s"
            % style.dim(
                "+%d more in %s" % (leftover, more_hint or "followups.md"),
                enabled=color,
            )
        )
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


def render_followups(
    findings: Iterable[Dict[str, Any]],
    *,
    seq: Optional[Dict[str, Any]] = None,
) -> str:
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
        status = seq_status_for(item, seq) if seq else ""
        mark = " **%s**" % status if status else ""
        lines.append("- **%s** [%s] %s%s%s" % (sev, kind, title, loc_s, mark))
    return "\n".join(lines) + "\n"


def render_seq_plan(
    queue: List[Dict[str, Any]],
    *,
    failed: str = "",
) -> str:
    lines = [
        "# Apply --seq plan",
        "",
        "One class at a time: architecture → test → implementation, then severity.",
        "Loop stops on the first class failure.",
        "",
    ]
    if failed:
        lines.append("Retry first (previous failure): `%s`" % failed)
        lines.append("")
    if not queue:
        lines.append("- (none remaining)")
        lines.append("")
        return "\n".join(lines)
    for i, item in enumerate(queue, 1):
        loc = item.get("path") or ""
        loc_s = " (`%s`)" % loc if loc else ""
        lines.append(
            "%d. `%s` **%s** [%s] %s%s"
            % (
                i,
                item.get("id") or finding_id(item),
                item.get("severity") or "?",
                item.get("kind") or "?",
                item.get("title") or "(untitled)",
                loc_s,
            )
        )
    lines.append("")
    return "\n".join(lines)


def render_seq_log(seq: Dict[str, Any], *, slug: str = "") -> str:
    lines = ["# Apply --seq", ""]
    steps = [row for row in as_list(seq.get("steps")) if isinstance(row, dict)]
    if not steps:
        lines.append("No classes applied yet.")
        lines.append("")
        return "\n".join(lines)
    for i, step in enumerate(steps, 1):
        fid = as_str(step.get("id")) or "?"
        lines.append("## %d. `%s` %s" % (i, fid, as_str(step.get("status")) or "?"))
        lines.append("")
        lines.append("- kind: %s" % (step.get("kind") or "?"))
        lines.append("- title: %s" % (step.get("title") or "(untitled)"))
        if step.get("path"):
            lines.append("- path: `%s`" % step["path"])
        if step.get("suite"):
            lines.append("- suite: %s" % step["suite"])
        hops = as_list(step.get("hops"))
        if hops:
            lines.append("- hops: %s" % ", ".join(as_str(h) for h in hops))
        lines.append("- artifacts: `seq/%s/`" % fid)
        lines.append("")
    failed = as_str(seq.get("failed"))
    resume = as_str(seq.get("resume"))
    stale = [as_str(x) for x in as_list(seq.get("stale")) if as_str(x)]
    cmd = "team apply %s --seq" % slug if slug else "team apply <slug> --seq"
    if failed:
        lines.extend(
            [
                "## Stopped",
                "",
                "Class `%s` failed. The worktree is left as this class left it (no rollback)."
                % failed,
                "",
                "- retry the same class: `%s`" % cmd,
                "- skip it and continue: `%s --skip-failed`" % cmd,
                "- reopen an earlier class: `%s --reopen <id>`" % cmd,
                "- review a finished class: `team review %s --seq %s`"
                % (slug or "<slug>", failed),
                "",
            ]
        )
    if resume:
        lines.extend(
            [
                "## Reopened",
                "",
                "Next `--seq` retries `%s`. Later classes are stale, not skipped." % resume,
                "",
            ]
        )
    if stale:
        lines.extend(
            [
                "## Stale",
                "",
                ", ".join("`%s`" % sid for sid in stale),
                "",
            ]
        )
    return "\n".join(lines)


def render_plan(
    groups: Dict[str, List[Dict[str, Any]]],
) -> str:
    lines = [
        "# Apply plan",
        "",
        "Order: design (architecture) → contract + tests → implementation → suite.",
        "Review and guardian run on team review, not apply.",
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
    suite_status: str,
    hops: List[str],
) -> str:
    lines = [
        "# Apply",
        "",
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
        key = finding_identity(item)
        path, title = key[0], key[1]
        if not title and not path:
            extras.append(item)
            continue
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = item
            order.append(key)
            continue
        # Same defect, two reviewers, two answers about kind: the classified
        # one wins. normalize_finding guarantees a kind, so "" is not the
        # unclassified case -- the word is.
        if kind_is_unclassified(prev.get("kind")) and not kind_is_unclassified(
            item.get("kind")
        ):
            by_key[key] = item
    return [by_key[k] for k in order] + extras
