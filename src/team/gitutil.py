from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from team.util import posix, under_root, write_text


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(proc.stderr.strip() or proc.stdout.strip() or "git failed")
    return proc.stdout


def is_git_repo(repo: Path) -> bool:
    """True only if repo is a git toplevel (not merely inside some parent tree)."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        return False
    try:
        return Path(proc.stdout.strip()).resolve() == repo.resolve()
    except OSError:
        return False


def head(repo: Path) -> str:
    out = git(repo, "rev-parse", "HEAD", check=False).strip()
    return out if out and "fatal" not in out.lower() else ""


def porcelain_paths(repo: Path) -> List[str]:
    out = git(repo, "status", "--porcelain", "-uall", check=False)
    paths = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        # XY PATH or XY ORIG -> PATH
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip().strip('"')
        paths.append(posix(rest))
    return sorted(set(paths))


def snapshot(repo: Path) -> dict:
    return {
        "head": head(repo),
        "paths": porcelain_paths(repo),
    }


def delta_paths(before: Sequence[str], after: Sequence[str]) -> List[str]:
    prior = set(before)
    return sorted(p for p in after if p not in prior)


def verify_delta(
    paths: Iterable[str],
    allowed_roots: Sequence[str],
    *,
    always_allowed: Sequence[str] = (),
) -> Tuple[List[str], List[str]]:
    """Return (ok, bad). Empty allowed_roots means advisory-only (all ok)."""
    ok: List[str] = []
    bad: List[str] = []
    always = list(always_allowed)
    roots = [r for r in allowed_roots if r]
    for path in paths:
        if any(under_root(path, root) for root in always):
            ok.append(path)
            continue
        if not roots:
            ok.append(path)
            continue
        if any(under_root(path, root) for root in roots):
            ok.append(path)
        else:
            bad.append(path)
    return ok, bad


def write_path_list(path: Path, paths: Sequence[str]) -> None:
    write_text(path, "\n".join(paths) + ("\n" if paths else ""))


def describe_verify(
    phase: str,
    delta: Sequence[str],
    bad: Sequence[str],
    allowed: Sequence[str],
) -> str:
    lines = [
        "phase: %s" % phase,
        "allowed_roots: %s" % (", ".join(allowed) if allowed else "(none — advisory)"),
        "delta (%d):" % len(delta),
    ]
    lines.extend("  %s" % p for p in delta)
    if bad:
        lines.append("violations:")
        lines.extend("  %s" % p for p in bad)
    return "\n".join(lines) + "\n"
