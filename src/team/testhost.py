from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from team.util import normalize_root


def discover_test_command(repo: Path, hint: str = "", test_root: str = "") -> str:
    """Discover how the host should run tests.

    ``hint`` is configured ``test_command``: the host suite. It wins the whole
    string. ``test_root`` is the write fence, not the suite — a nested root
    only supplies the existence check for the python fallback (cwd,
    PYTHONPATH, and selected dirs are not invented). ``pytest.ini`` /
    ``pyproject.toml`` / ``setup.cfg`` only select the python runner; the
    command is the same helper as the no-manifest fallback. Empty
    ``test_root`` keeps the ``tests/`` convention.
    """
    if hint:
        return hint
    makefile = repo / "Makefile"
    if makefile.is_file():
        text = makefile.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^test[s]?:", text, re.M):
            return "make test"
    pkg = repo / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        scripts = data.get("scripts") or {}
        if "test" in scripts:
            return "npm test"
    python_start = _python_fallback_root(test_root)
    for name in ("pytest.ini", "pyproject.toml", "setup.cfg"):
        path = repo / name
        if path.is_file() and "pytest" in path.read_text(encoding="utf-8", errors="replace"):
            return _python_test_command(python_start)
    if (repo / "Cargo.toml").is_file():
        return "cargo test"
    if (repo / "go.mod").is_file():
        return "go test ./..."
    test_dir = repo / python_start
    if test_dir.is_dir() and any(test_dir.rglob("test_*.py")):
        return _python_test_command(python_start)
    return ""


def _python_fallback_root(test_root: Any = "") -> str:
    """Configured test_root, or the labeled tests/ convention.

    write-code deny derives this same name: unset/empty/whitespace
    ``test_root`` still names ``tests``.
    """
    return normalize_root(test_root) or "tests"


def _python_test_command(start: str = "tests") -> str:
    """Python host command. Never invents a selected-dir suite.

    The labeled ``tests`` convention may use unittest ``discover -s tests``.
    Any other start is not a suite path: pytest stays bare, unittest is
    not invented. Nested packages must set ``test_command``.
    """
    labeled = (start or "tests") == "tests"
    try:
        import pytest  # noqa: F401

        return "python3 -m pytest -q"
    except ImportError:
        if labeled:
            return "python3 -m unittest discover -s tests -q"
        return ""


# Unittest finished banners. These beat collection phrases reprinted inside
# AssertionError dumps. Pytest's "N error in Xs" underline is *also* printed
# after collection death, so it is not a veto for collection_failed.
_UNITTEST_FINISHED = (
    re.compile(r"^Ran \d+ tests?\b", re.M),
    re.compile(r"^FAILED \(failures=\d+", re.M),
)
_PYTEST_FINISHED = (
    re.compile(
        r"^={5,}.*\b\d+\s+(failed|passed|error|errors|skipped)\b", re.M | re.I
    ),
)
# A finished runner summary. Used to describe a completed run, not to decide
# collection death (see collection_failed).
_SUITE_COMPLETED = _UNITTEST_FINISHED + _PYTEST_FINISHED

# unittest summary, not a case name: "FAILED (failures=3)"
_FAILED_SUMMARY = re.compile(r"^FAILED\s+\(")
_MAX_FAIL_NAME = 200


def suite_completed(output: str) -> bool:
    """True when the runner printed a finished-suite banner.

    Collection death does not print ``Ran N tests`` / ``FAILED (failures=N)``
    / a pytest result underline. Those banners win over phrase matches in
    dumped source.
    """
    text = output or ""
    return any(pat.search(text) for pat in _SUITE_COMPLETED)


def runner_unavailable(output: str, exit_code: Optional[int] = None) -> bool:
    """True when the host never started a suite runner (not a product FAIL).

    POSIX 127 / ``command not found`` is the missing-runner class. Other
    nonzero exits are not this predicate.
    """
    if exit_code == 127:
        return True
    text = output or ""
    return bool(re.search(r": command not found\b", text))


def collection_failed(output: str, exit_code: Optional[int] = None) -> bool:
    """True when the runner died before executing cases (not a product FAIL).

    Language only. Exit 4/5 are pytest usage / no-tests-collected; make,
    npm, and wrappers reuse those codes for product failure. ``exit_code``
    is accepted from callers and is not a collection-death signal.

    Unittest ``Ran N tests`` / ``FAILED (failures=N)`` beat collection
    phrases reprinted in assertion dumps. Pytest collection death prints
    ``ERROR collecting`` in an underlined header and ``1 error during
    collection`` (singular) plus the same ``N error in Xs`` underline a
    finished error-run uses — that underline is not a veto. Jest / make
    death is an unlisted approximation and stays open (not this predicate).
    """
    text = output or ""
    # unittest wraps import/load death as a synthetic case, then prints Ran 1.
    if re.search(r"unittest\.loader\._FailedTest", text):
        return True
    if any(pat.search(text) for pat in _UNITTEST_FINISHED):
        return False
    if re.search(r"errors? during collection", text, re.I):
        return True
    if re.search(r"ERROR collecting\b", text):
        return True
    if re.search(r"ImportError while loading conftest", text):
        return True
    if re.search(r"ERROR:\s+found no collectors", text, re.I):
        return True
    if re.search(r"^pytest:\s+error:", text, re.M):
        return True
    return False


def is_product_fail(run: Optional[Dict] = None, *, status: str = "") -> bool:
    """True when the host proved a product FAIL.

    PASS, FAIL, and UNVERIFIED are three outcomes. Missing command,
    collection death, and timeout are UNVERIFIED — not a class failure
    and not repair.
    """
    if run is not None:
        status = str(run.get("status") or "")
    return status == "FAIL"


def needs_repair(run: Optional[Dict]) -> bool:
    """Debugger owns a product FAIL. Collection / no-command / timeout is UNVERIFIED."""
    run = run or {}
    if run.get("collection_failed"):
        return False
    return is_product_fail(run)


def run_suite(repo: Path, command: str, timeout: int = 1800) -> Dict:
    if not command:
        return {
            "command": "",
            "exit": None,
            "status": "UNVERIFIED",
            "output": "(no test command discovered)",
            "failing": [],
            "names_unparsed": False,
            "collection_failed": False,
            "runner_missing": False,
        }
    try:
        proc = subprocess.run(
            command,
            cwd=str(repo),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout if timeout > 0 else None,
        )
    except subprocess.TimeoutExpired as exc:
        output = _timeout_output(exc)
        return {
            "command": command,
            "exit": None,
            "status": "UNVERIFIED",
            "output": output[-20000:],
            "failing": [],
            "names_unparsed": False,
            "collection_failed": False,
            "runner_missing": False,
        }
    output = proc.stdout or ""
    failing = parse_failing_names(output)
    collected = collection_failed(output, proc.returncode)
    missing = runner_unavailable(output, proc.returncode)
    if proc.returncode == 0:
        status = "PASS"
    elif collected or missing:
        # The suite did not run. Names parsed from a collection traceback
        # (AssertionError: in conftest) are not product FAILs.
        status = "UNVERIFIED"
    else:
        status = "FAIL"
    names_unparsed = status == "FAIL" and not failing
    return {
        "command": command,
        "exit": proc.returncode,
        "status": status,
        "output": output[-20000:],
        "failing": failing,
        "names_unparsed": names_unparsed,
        "collection_failed": collected,
        "runner_missing": missing,
    }


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    raw = exc.stdout
    if raw is None:
        return "(suite timed out)"
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
    return text or "(suite timed out)"


def parse_failing_names(output: str) -> List[str]:
    names: List[str] = []
    patterns = [
        re.compile(r"^FAILED\s+(\S+)", re.M),
        re.compile(r"^FAIL:\s+(\S+)", re.M),
        re.compile(r"^--- FAIL:\s+(\S+)", re.M),
        re.compile(r"^not ok \d+\s+-?\s*(.+)$", re.M),
    ]
    for pat in patterns:
        for match in pat.finditer(output):
            names.append(match.group(1) if match.lastindex else match.group(0))
    # unique, preserve order
    seen: Set[str] = set()
    out: List[str] = []
    for name in names:
        if _FAILED_SUMMARY.match("FAILED %s" % name) or name.startswith("("):
            continue
        if len(name) > _MAX_FAIL_NAME:
            name = name[:_MAX_FAIL_NAME] + "…"
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out[:50]


def compare(baseline: Dict, final: Dict) -> Dict:
    base_fail = set(baseline.get("failing") or [])
    final_fail = set(final.get("failing") or [])
    new = sorted(final_fail - base_fail)
    gone = sorted(base_fail - final_fail)
    preexisting = sorted(final_fail & base_fail)
    base_status = baseline.get("status") or "UNVERIFIED"
    final_status = final.get("status") or "UNVERIFIED"
    names_unparsed = _fail_names_unparsed(final)
    if final_status == "UNVERIFIED":
        # Only a side that did not run is UNVERIFIED. A later product FAIL
        # is still a FAIL when the baseline never ran.
        verdict = "UNVERIFIED"
    elif final_status == "PASS" and base_status == "FAIL":
        verdict = "PASS"
    elif final_status == "PASS":
        verdict = "PASS"
    elif names_unparsed:
        # Empty parsed sets are not a known fail-set; never REGRESSION-from-∅.
        verdict = "FAIL"
    elif new:
        verdict = "REGRESSION"
    elif preexisting and not new:
        verdict = "BROKEN_BASELINE"
    else:
        verdict = "FAIL"
    out = {
        "verdict": verdict,
        "new_failures": new,
        "preexisting_failures": preexisting,
        "fixed_failures": gone,
        "baseline_status": base_status,
        "final_status": final_status,
    }
    if names_unparsed:
        out["names_unparsed"] = True
    return out


def _fail_names_unparsed(final: Dict) -> bool:
    if (final.get("status") or "") != "FAIL":
        return False
    if final.get("names_unparsed") or final.get("failing_unknown") or final.get("unparsed"):
        return True
    return not list(final.get("failing") or [])


def render_report(title: str, run: Dict, comparison: Optional[Dict] = None) -> str:
    lines = [
        "# %s" % title,
        "",
        "- command: `%s`" % (run.get("command") or "(none)"),
        "- exit: %s" % run.get("exit"),
        "- status: %s" % run.get("status"),
    ]
    if run.get("collection_failed"):
        lines.append("- collection: failed (suite did not run; not a product FAIL)")
    if run.get("runner_missing"):
        lines.append("- runner: unavailable (suite did not run; not a product FAIL)")
    if comparison:
        lines.append("- verdict: **%s**" % comparison.get("verdict"))
        if comparison.get("names_unparsed"):
            lines.append("- failing names: unparsed (unknown; not an empty fail-set)")
        # Parens matter: % binds tighter than or, so without them an empty
        # new-failures list renders as "- new failures: " and the "(none)"
        # fallback is unreachable -- in the authoritative test report.
        lines.append(
            "- new failures: %s"
            % (", ".join(comparison.get("new_failures") or []) or "(none)")
        )
        lines.append(
            "- preexisting failures: %s"
            % (", ".join(comparison.get("preexisting_failures") or []) or "(none)")
        )
        lines.append(
            "- fixed since baseline: %s"
            % (", ".join(comparison.get("fixed_failures") or []) or "(none)")
        )
    failing = run.get("failing") or []
    if failing:
        lines.append("")
        lines.append("## Failing names")
        lines.extend("- %s" % n for n in failing)
    output = run.get("output") or ""
    if output:
        lines.append("")
        lines.append("## Log excerpt")
        lines.append("```")
        lines.append(output[-8000:])
        lines.append("```")
    return "\n".join(lines) + "\n"
