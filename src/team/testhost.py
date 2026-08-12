from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set


def discover_test_command(repo: Path, hint: str = "") -> str:
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
    for name in ("pytest.ini", "pyproject.toml", "setup.cfg"):
        path = repo / name
        if path.is_file() and "pytest" in path.read_text(encoding="utf-8", errors="replace"):
            return _python_test_command(repo)
    if (repo / "tests").is_dir() and any((repo / "tests").rglob("test_*.py")):
        return _python_test_command(repo)
    if (repo / "Cargo.toml").is_file():
        return "cargo test"
    if (repo / "go.mod").is_file():
        return "go test ./..."
    return ""


def _python_test_command(repo: Path) -> str:
    try:
        import pytest  # noqa: F401

        return "python3 -m pytest -q"
    except ImportError:
        return "python3 -m unittest discover -s tests -q"


def run_suite(repo: Path, command: str, timeout: int = 1800) -> Dict:
    if not command:
        return {
            "command": "",
            "exit": None,
            "status": "UNVERIFIED",
            "output": "(no test command discovered)",
            "failing": [],
        }
    proc = subprocess.run(
        command,
        cwd=str(repo),
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout if timeout > 0 else None,
    )
    output = proc.stdout or ""
    failing = parse_failing_names(output)
    status = "PASS" if proc.returncode == 0 else "FAIL"
    return {
        "command": command,
        "exit": proc.returncode,
        "status": status,
        "output": output[-20000:],
        "failing": failing,
    }


def parse_failing_names(output: str) -> List[str]:
    names: List[str] = []
    patterns = [
        re.compile(r"^FAILED\s+(\S+)", re.M),
        re.compile(r"^FAIL:\s+(\S+)", re.M),
        re.compile(r"^--- FAIL:\s+(\S+)", re.M),
        re.compile(r"^not ok \d+\s+-?\s*(.+)$", re.M),
        re.compile(r"^AssertionError:.*", re.M),
    ]
    for pat in patterns:
        for match in pat.finditer(output):
            names.append(match.group(1) if match.lastindex else match.group(0))
    # unique, preserve order
    seen: Set[str] = set()
    out: List[str] = []
    for name in names:
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
    if final_status == "UNVERIFIED" or base_status == "UNVERIFIED":
        verdict = "UNVERIFIED"
    elif final_status == "PASS" and base_status == "FAIL":
        verdict = "PASS"
    elif final_status == "PASS":
        verdict = "PASS"
    elif new:
        verdict = "REGRESSION"
    elif preexisting and not new:
        verdict = "BROKEN_BASELINE"
    else:
        verdict = "FAIL"
    return {
        "verdict": verdict,
        "new_failures": new,
        "preexisting_failures": preexisting,
        "fixed_failures": gone,
        "baseline_status": base_status,
        "final_status": final_status,
    }


def render_report(title: str, run: Dict, comparison: Optional[Dict] = None) -> str:
    lines = [
        "# %s" % title,
        "",
        "- command: `%s`" % (run.get("command") or "(none)"),
        "- exit: %s" % run.get("exit"),
        "- status: %s" % run.get("status"),
    ]
    if comparison:
        lines.append("- verdict: **%s**" % comparison.get("verdict"))
        lines.append("- new failures: %s" % ", ".join(comparison.get("new_failures") or []) or "(none)")
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
