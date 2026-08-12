from __future__ import annotations

import subprocess
import time
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


REVIEWED_PREFIX = "reviewed-"


def last_dedicated_tag(repo: Path) -> str:
    """Newest `reviewed-*` tag reachable from HEAD (vibe.rc gittag)."""
    out = git(
        repo,
        "tag",
        "--list",
        REVIEWED_PREFIX + "*",
        "--merged",
        "HEAD",
        "--sort=-creatordate",
        check=False,
    )
    for line in out.splitlines():
        tag = line.strip()
        if tag:
            return tag
    return ""


def last_any_tag(repo: Path) -> str:
    out = git(repo, "describe", "--tags", "--abbrev=0", check=False).strip()
    if not out or "fatal" in out.lower() or "error" in out.lower():
        return ""
    return out


def resolve_review_base(repo: Path, since: str = "") -> Tuple[str, str]:
    """Return (base_ref, kind) where kind is dedicated | tag | since | branch."""
    if since:
        return since, "since"
    dedicated = last_dedicated_tag(repo)
    if dedicated:
        return dedicated, "dedicated"
    any_tag = last_any_tag(repo)
    if any_tag:
        return any_tag, "tag"
    return "", "branch"


def range_log(repo: Path, base: str) -> str:
    if base:
        return git(repo, "log", "--oneline", "%s..HEAD" % base, check=False)
    return git(repo, "log", "--oneline", check=False)


def range_diff(repo: Path, base: str) -> str:
    if base:
        return git(repo, "diff", "%s..HEAD" % base, check=False)
    root = git(repo, "rev-list", "--max-parents=0", "HEAD", check=False).strip().splitlines()
    if root:
        return git(repo, "diff", root[0], "HEAD", check=False)
    return git(repo, "diff", check=False)


def range_name_only(repo: Path, base: str) -> List[str]:
    if base:
        out = git(repo, "diff", "--name-only", "%s..HEAD" % base, check=False)
    else:
        out = git(repo, "diff", "--name-only", check=False)
    return [posix(p) for p in out.splitlines() if p.strip()]


def commit_count(repo: Path, base: str) -> int:
    if base:
        out = git(repo, "rev-list", "--count", "%s..HEAD" % base, check=False).strip()
    else:
        out = git(repo, "rev-list", "--count", "HEAD", check=False).strip()
    try:
        return int(out)
    except ValueError:
        return 0


def describe_range(base: str, kind: str, count: int) -> str:
    if kind == "dedicated":
        return "all %d commit(s) since dedicated tag %s" % (count, base)
    if kind == "tag":
        return "all %d commit(s) since tag %s" % (count, base)
    if kind == "since":
        return "all %d commit(s) since %s" % (count, base)
    if kind == "pr":
        return "PR %s (%d commit(s) in the collected diff)" % (base, count)
    return "all %d commit(s) in this branch (no tags found)" % count


def pr_bundle(repo: Path, pr: str) -> Tuple[str, str, str]:
    """Return (log, diff, how). Prefer gh; else merge-base with main/master."""
    gh = subprocess.run(
        ["gh", "pr", "diff", str(pr)],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    log = ""
    view = subprocess.run(
        ["gh", "pr", "view", str(pr), "--json", "title,commits"],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if view.returncode == 0:
        log = view.stdout
    if gh.returncode == 0 and (gh.stdout or "").strip():
        return log, gh.stdout, "gh"
    for branch in ("main", "master", "origin/main", "origin/master"):
        mb = git(repo, "merge-base", branch, "HEAD", check=False).strip()
        if mb and "fatal" not in mb.lower():
            return (
                git(repo, "log", "--oneline", "%s..HEAD" % mb, check=False),
                git(repo, "diff", "%s...HEAD" % mb, check=False),
                "merge-base:%s" % branch,
            )
    return range_log(repo, ""), range_diff(repo, ""), "branch-fallback"


def stamp_reviewed(repo: Path) -> str:
    """Create reviewed-YYYYMMDD-HHMM like vibe.rc gittag."""
    base = REVIEWED_PREFIX + time.strftime("%Y%m%d-%H%M")
    name = base
    n = 2
    while True:
        exists = git(repo, "rev-parse", "-q", "--verify", "refs/tags/%s" % name, check=False)
        if not exists.strip():
            break
        name = "%s-%d" % (base, n)
        n += 1
    git(repo, "tag", name)
    return name
