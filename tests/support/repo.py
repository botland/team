"""Git fixtures for contract tests."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "git failed")
    return proc.stdout


def init_repo(root: Path, readme: str = "one\n") -> None:
    git(root, "init")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "README").write_text(readme, encoding="utf-8")
    git(root, "add", "README")
    git(root, "commit", "-m", "first")


def commit_file(root: Path, rel: str, text: str, msg: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(root, "add", "--", rel)
    git(root, "commit", "-m", msg)


def head_sha(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").strip()
