from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from team.util import posix, under_root, write_text

# git hash-object -t tree /dev/null — the empty tree, stable across git versions.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_DIFF_GIT = re.compile(r"^diff --git a/(.+) b/(.+)$", re.M)


@dataclass
class RangeSpec:
    base_sha: str
    head_sha: str
    kind: str
    source: str
    base_label: str


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


def _content_id(repo: Path, rel: str) -> str:
    path = repo / rel
    if not path.exists():
        return "missing"
    if path.is_dir():
        return "dir"
    try:
        data = path.read_bytes()
    except OSError:
        return "unreadable"
    return "sha256:" + hashlib.sha256(data).hexdigest()


def snapshot(repo: Path) -> dict:
    paths = porcelain_paths(repo)
    entries = {p: _content_id(repo, p) for p in paths}
    return {
        "head": head(repo),
        "paths": paths,
        "entries": entries,
    }


def delta_paths(before: Sequence[str], after: Sequence[str]) -> List[str]:
    prior = set(before)
    return sorted(p for p in after if p not in prior)


def product_paths(paths: Iterable[str]) -> List[str]:
    """Fence paths that are not orchestrator work files."""
    out = []
    for path in paths:
        rel = posix(path)
        if not rel or rel.startswith(".team/"):
            continue
        out.append(rel)
    return out


def worktree_diff(repo: Path, paths: Sequence[str]) -> str:
    """Unified diff of product paths vs HEAD, including new untracked files."""
    chunks: List[str] = []
    for rel in product_paths(paths):
        tracked = git(repo, "diff", "HEAD", "--", rel, check=False)
        if tracked.strip():
            chunks.append(tracked if tracked.endswith("\n") else tracked + "\n")
            continue
        path = repo / rel
        if not path.is_file():
            continue
        listed = git(repo, "ls-files", "--", rel, check=False).strip()
        if listed:
            continue
        proc = subprocess.run(
            ["git", "-C", str(repo), "diff", "--no-index", "--", "/dev/null", rel],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.stdout.strip():
            chunks.append(proc.stdout if proc.stdout.endswith("\n") else proc.stdout + "\n")
    return "".join(chunks)


def changed_paths(repo: Path, before: dict, after: dict) -> List[str]:
    """Every path whose content, existence, or committed state differs."""
    before = before or {}
    after = after or {}
    b_entries = dict(before.get("entries") or {})
    a_entries = dict(after.get("entries") or {})
    found = set()
    for path in set(b_entries) | set(a_entries):
        if b_entries.get(path) != a_entries.get(path):
            found.add(path)
    before_head = before.get("head") or ""
    after_head = after.get("head") or ""
    if before_head and after_head and before_head != after_head:
        out = git(repo, "diff", "--name-only", before_head, after_head, check=False)
        for line in out.splitlines():
            rel = posix(line.strip())
            if rel:
                found.add(rel)
    return sorted(found)


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


def already_dirty_mutations(
    delta: Iterable[str],
    origin_entries: Dict[str, str],
    before_entries: Dict[str, str],
    after_entries: Dict[str, str],
    *,
    exempt_roots: Sequence[str] = (),
) -> List[str]:
    """Paths this hop mutated that were already dirty when the run started.

    Dirt from earlier hops of the same run is not a hide: repair and later
    ``apply --seq`` classes must be able to edit those files under their root.
    """
    found: List[str] = []
    exempt = [r for r in exempt_roots if r]
    for path in delta:
        if path not in origin_entries:
            continue
        if path not in before_entries:
            continue
        if before_entries.get(path) == after_entries.get(path):
            continue
        if any(under_root(path, root) for root in exempt):
            continue
        found.append(path)
    return found


def write_path_list(path: Path, paths: Sequence[str]) -> None:
    write_text(path, "\n".join(paths) + ("\n" if paths else ""))


def describe_verify(
    phase: str,
    delta: Sequence[str],
    bad: Sequence[str],
    allowed: Sequence[str],
    head_before: str = "",
    head_after: str = "",
    already_dirty: Sequence[str] = (),
) -> str:
    lines = [
        "phase: %s" % phase,
        "allowed_roots: %s" % (", ".join(allowed) if allowed else "(none — advisory)"),
        "delta (%d):" % len(delta),
    ]
    lines.extend("  %s" % p for p in delta)
    if head_before or head_after:
        lines.append("head: %s -> %s" % (head_before or "?", head_after or "?"))
        if head_before and head_after and head_before != head_after:
            lines.append("commit: HEAD changed during phase")
    if bad:
        lines.append("violations:")
        lines.extend("  %s" % p for p in bad)
    if already_dirty:
        lines.append("already_dirty (since run start):")
        lines.extend("  %s" % p for p in already_dirty)
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
        return git(repo, "diff", "-M", "%s..HEAD" % base, check=False)
    # Combined empty-tree diff hides rename old paths. log -p --root keeps
    # ordinary diff --git headers for every commit, including the root and renames.
    return git(repo, "log", "-p", "-M", "--root", check=False)


def paths_from_diff(patch: str) -> List[str]:
    found = set()
    for a, b in _DIFF_GIT.findall(patch or ""):
        for side in (a, b):
            rel = posix(side.strip())
            if rel and rel != "/dev/null":
                found.add(rel)
    return sorted(found)


def range_name_only(repo: Path, base: str) -> List[str]:
    return paths_from_diff(range_diff(repo, base))


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
    log = ""
    try:
        gh = subprocess.run(
            ["gh", "pr", "diff", str(pr)],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        view = subprocess.run(
            ["gh", "pr", "view", str(pr), "--json", "title,commits"],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        gh = None
        view = None
    if view is not None and view.returncode == 0:
        log = view.stdout
    if gh is not None and gh.returncode == 0 and (gh.stdout or "").strip():
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


def resolve_commit(repo: Path, ref: str) -> str:
    out = git(repo, "rev-parse", "--verify", "%s^{commit}" % ref, check=False).strip()
    if not out or "fatal" in out.lower():
        raise GitError("cannot resolve %s" % ref)
    return out


def is_ancestor(repo: Path, ancestor: str, descendant: str = "HEAD") -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode == 0


def stamp_reviewed(repo: Path, ref: str = "HEAD", message: str = "") -> str:
    """Create an annotated reviewed-YYYYMMDD-HHMM tag pointing at ref."""
    commit = resolve_commit(repo, ref)
    base = REVIEWED_PREFIX + time.strftime("%Y%m%d-%H%M")
    name = base
    n = 2
    while True:
        exists = git(repo, "rev-parse", "-q", "--verify", "refs/tags/%s" % name, check=False)
        if not exists.strip():
            break
        name = "%s-%d" % (base, n)
        n += 1
    body = message or ("reviewed %s" % commit)
    git(repo, "tag", "-a", name, "-m", body, commit)
    return name


def pr_head_oid(repo: Path, pr: str) -> Tuple[str, str]:
    """Return (headRefOid, how). how is gh or a failure token."""
    try:
        view = subprocess.run(
            ["gh", "pr", "view", str(pr), "--json", "headRefOid"],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return "", "gh-missing"
    if view.returncode != 0:
        return "", "gh-error"
    try:
        data = json.loads(view.stdout or "{}")
    except json.JSONDecodeError:
        return "", "gh-error"
    oid = str((data or {}).get("headRefOid") or "").strip()
    if not oid:
        return "", "gh-error"
    return oid, "gh"


def delete_reviewed_tag(repo: Path, name: str) -> str:
    tag = (name or "").strip()
    if not tag.startswith(REVIEWED_PREFIX):
        raise GitError("refusing to delete non-reviewed tag %s" % (name or "(empty)"))
    git(repo, "tag", "-d", tag)
    return tag


def list_reviewed_tags(repo: Path) -> List[Dict[str, object]]:
    """All reviewed-* tags (not only those merged to HEAD)."""
    out = git(
        repo,
        "for-each-ref",
        "--sort=-creatordate",
        "--format=%(refname:short)\t%(objectname:short)\t%(creatordate:short)",
        "refs/tags/%s*" % REVIEWED_PREFIX,
        check=False,
    )
    current, _kind = resolve_review_base(repo)
    rows: List[Dict[str, object]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        tag = (parts[0] if parts else "").strip()
        if not tag:
            continue
        commit = parts[1].strip() if len(parts) > 1 else ""
        date = parts[2].strip() if len(parts) > 2 else ""
        rows.append(
            {
                "tag": tag,
                "commit": commit,
                "date": date,
                "commits_ahead": commit_count(repo, tag),
                "current": tag == current,
                "ancestor": is_ancestor(repo, tag),
            }
        )
    return rows
