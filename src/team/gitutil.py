from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from team.util import escapes_repo, normalize_root, posix, under_root, write_text

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


def committed_blob(repo: Path, rel: str, ref: str = "HEAD") -> str:
    """File contents at ``ref``, or empty if that path is not in the tree."""
    path = posix(rel).lstrip("/")
    if not path or ".." in path.split("/"):
        return ""
    return git(repo, "show", "%s:%s" % (ref, path), check=False)


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


def _unquote_git_path(raw: str) -> str:
    text = (raw or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return posix(text)


def _status_code(line: str) -> str:
    """Primary letter on a porcelain or name-status line (R, C, M, D, …)."""
    text = (line or "").lstrip()
    if not text:
        return ""
    if text[0] in "RCMADU" and len(text) > 1 and text[1].isdigit():
        return text[0]
    xy = (line or "")[:2]
    if "R" in xy:
        return "R"
    if "C" in xy:
        return "C"
    return text[0]


def paths_from_status_line(line: str) -> List[str]:
    """Every path this porcelain / name-status line actually changed.

    Rename (`R`, including `R100`) names source and dest. Copy (`C` / `C075`)
    names only the dest — the source still exists. Plain add/modify/delete
    name the one path. This is the pair: status text ↔ worktree change.
    """
    raw = (line or "").rstrip("\n")
    if not raw.strip():
        return []
    if raw.startswith("!!"):
        rest = raw[2:].lstrip()
        if " -> " in rest:
            left, right = rest.split(" -> ", 1)
            return [p for p in (_unquote_git_path(left), _unquote_git_path(right)) if p]
        path = _unquote_git_path(rest)
        return [path] if path else []
    code = _status_code(raw)
    named: List[str] = []
    if "\t" in raw:
        parts = raw.split("\t")
        named = [_unquote_git_path(p) for p in parts[1:] if p.strip()]
    else:
        match = re.match(r"^[ A-Z?]{1,2}\d*\s+(.*)$", raw)
        rest = match.group(1) if match else (raw[3:] if len(raw) >= 4 else raw)
        if " -> " in rest:
            left, right = rest.split(" -> ", 1)
            named = [_unquote_git_path(left), _unquote_git_path(right)]
        else:
            path = _unquote_git_path(rest)
            if path:
                named = [path]
    named = [p for p in named if p]
    if not named:
        return []
    if code == "C" and len(named) >= 2:
        return [named[-1]]
    return named


def _is_team_work(rel: str) -> bool:
    """Orchestrator work dir — exempt from product-tree membership."""
    return under_root(rel, ".team/work")


def porcelain_paths(repo: Path) -> List[str]:
    """Dirty paths: create / modify / delete, including gitignored product files.

    Snapshot, changed_paths, verify, and restore share this membership.
    Ignored ``.team/work`` stays protocol, not product; non-ignored dirty
    paths (including ``.team/work`` when it is not ignored) stay visible.
    """
    paths = set()
    out = git(repo, "status", "--porcelain", "-uall", check=False)
    for line in out.splitlines():
        paths.update(paths_from_status_line(line))
    for rel in ignored_untracked(repo):
        if not _is_team_work(rel):
            paths.add(rel)
    return sorted(paths)


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
    """In-repo fence membership. Extra-worktree lives on the hop snapshot."""
    repo = Path(repo)
    if is_git_repo(repo):
        paths = porcelain_paths(repo)
        head_val = head(repo)
    else:
        paths = tree_paths(repo)
        head_val = ""
    entries = {p: _content_id(repo, p) for p in paths}
    return {
        "head": head_val,
        "paths": paths,
        "entries": entries,
    }


def tree_paths(repo: Path) -> List[str]:
    """Every file under ``repo`` except ``.git`` and orchestrator work.

    This is the non-git observer. A git repo still uses porcelain for dirty
    membership; without ``.git`` the whole tree is the membership.
    """
    try:
        repo_r = repo.resolve()
    except OSError:
        return []
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_r, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            full = Path(dirpath) / name
            try:
                rel = posix(str(full.relative_to(repo_r)))
            except ValueError:
                continue
            if not rel or _is_team_work(rel) or rel == ".git" or rel.startswith(".git/"):
                continue
            found.append(rel)
    return sorted(found)


def _path_is_outside(repo: Path, path: Path) -> bool:
    """True when ``path`` is not inside ``repo`` (extra-worktree)."""
    try:
        repo_r = repo.resolve()
        resolved = path.resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(repo_r)
        return False
    except ValueError:
        return True


def _relpath_from_repo(repo: Path, path: Path) -> str:
    try:
        rel = os.path.relpath(str(path), str(repo.resolve()))
    except (OSError, ValueError):
        return posix(str(path))
    return posix(rel)


# Extra-worktree leaves named by product law (AGENTS.md: no ~/vibe.rc,
# no ~/.team). Bases are --repo's parent and $HOME. Not a growing
# attack-path list; arbitrary other extra-repo paths stay open (need a
# sandbox). Tests write vibe.rc and ../vibe.rc — both are this set.
_OUTSIDE_LEAVES = ("vibe.rc", ".team")


def _iter_outside_bases(repo: Path) -> List[Path]:
    """Directories that may hold an R-named extra-worktree leaf."""
    bases: List[Path] = []
    seen = set()

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        bases.append(resolved)

    try:
        add(repo.resolve().parent)
    except OSError:
        pass
    try:
        add(Path.home())
    except OSError:
        pass
    return bases


def _record_outside_leaf(repo: Path, child: Path, entries: Dict[str, str]) -> None:
    if not child.exists() and not child.is_symlink():
        return
    rel = _relpath_from_repo(repo, child)
    if not rel:
        return
    if child.is_symlink() or child.is_file():
        entries[rel] = _content_id(repo, rel)
        return
    if child.is_dir():
        _record_outside_tree(repo, child, entries)


def _record_outside_tree(repo: Path, directory: Path, entries: Dict[str, str]) -> None:
    """Record a named directory leaf and every nested path under it.

    The observer space is the product-law pair of leaves × bases × the
    whole tree under a directory leaf. A ``dir`` token plus immediate
    files is not the leaf — nested creates stay invisible unless walked.
    Symlink children are recorded as files and not followed.
    """
    rel = _relpath_from_repo(repo, directory)
    if rel:
        entries[rel] = "dir"
    try:
        children = list(directory.iterdir())
    except OSError:
        return
    for sub in children:
        sub_rel = _relpath_from_repo(repo, sub)
        if not sub_rel:
            continue
        try:
            if sub.is_symlink() or sub.is_file():
                entries[sub_rel] = _content_id(repo, sub_rel)
            elif sub.is_dir():
                _record_outside_tree(repo, sub, entries)
        except OSError:
            continue


def outside_snapshot(repo: Path) -> Dict[str, str]:
    """Content ids of R-named extra-worktree leaves (``vibe.rc``, ``.team``).

    A file leaf is that path. A directory leaf is the whole tree under
    that name (nested files and directories), not a ``dir`` token plus
    immediate files. Keys are posix paths relative to ``repo``
    (``../.team/work/x``). Missing leaves are omitted so a create
    becomes an after-only key. Hop-local fence state — not the in-repo
    dirty snapshot persisted at run start.
    """
    try:
        repo_r = repo.resolve()
    except OSError:
        return {}
    entries: Dict[str, str] = {}
    for base in _iter_outside_bases(repo):
        for name in _OUTSIDE_LEAVES:
            child = base / name
            if not _path_is_outside(repo_r, child):
                continue
            _record_outside_leaf(repo, child, entries)
    return entries


def outside_blobs(repo: Path) -> Dict[str, Optional[bytes]]:
    """Pre-hop bytes for extra-worktree files. Missing/unreadable → None."""
    blobs: Dict[str, Optional[bytes]] = {}
    for rel in outside_snapshot(repo):
        path = repo / rel
        if path.is_file() or path.is_symlink():
            try:
                blobs[rel] = path.read_bytes()
            except OSError:
                blobs[rel] = None
        else:
            blobs[rel] = None
    return blobs


def outside_changed(
    before: Optional[Dict[str, str]], after: Optional[Dict[str, str]]
) -> List[str]:
    """Extra-worktree paths whose content or existence differs."""
    before = before or {}
    after = after or {}
    found = []
    for path in set(before) | set(after):
        if before.get(path) != after.get(path):
            found.append(path)
    return sorted(found)


def revert_outside(repo: Path, before: dict) -> None:
    """Restore extra-worktree create/modify/delete to ``before``.

    Never touches a path inside ``--repo``. New files are unlinked.
    Modified files return from stored blobs. Nested creates, modifies,
    and deletes under a directory leaf restore the same way. New empty
    directories are removed; a pre-existing foreign directory is left
    in place.
    """
    before = before or {}
    if "outside" not in before:
        return
    before_out = dict(before.get("outside") or {})
    blobs = dict(before.get("outside_blobs") or {})
    after_out = outside_snapshot(repo)
    try:
        repo_r = repo.resolve()
    except OSError:
        return
    # Files before directories so a new `.team/foo` is unlinked before rmdir.
    for rel in sorted(outside_changed(before_out, after_out), key=lambda p: (-p.count("/"), p)):
        path = repo / rel
        if not _path_is_outside(repo_r, path):
            continue
        if rel in blobs:
            data = blobs[rel]
            if data is None:
                if path.is_file() or path.is_symlink():
                    path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            continue
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir() and rel not in before_out:
            try:
                path.rmdir()
            except OSError:
                pass


def worktree_blobs(repo: Path) -> Dict[str, Optional[bytes]]:
    """Working-tree bytes for currently dirty (or non-git tree) paths."""
    blobs: Dict[str, Optional[bytes]] = {}
    rels = porcelain_paths(repo) if is_git_repo(repo) else tree_paths(repo)
    for rel in rels:
        path = repo / rel
        if path.is_file() or path.is_symlink():
            try:
                blobs[rel] = path.read_bytes()
            except OSError:
                blobs[rel] = None
        else:
            blobs[rel] = None
    return blobs


def revert_product(
    repo: Path,
    before: dict,
    *,
    skip_prefixes: Sequence[str] = (".team/work",),
) -> None:
    """Revert product-tree create/modify/delete/commit to ``before``.

    Not unlink-only: tracked files return via git/HEAD; hop-start dirty
    files return from stored blobs. ``.team/work`` is left alone.
    """
    before = before or {}
    before_head = str(before.get("head") or "")
    after_head = head(repo)
    head_moved = bool(before_head and after_head and before_head != after_head)
    reset_error: Optional[GitError] = None
    if head_moved:
        try:
            git(repo, "reset", "--mixed", before_head, check=True)
        except GitError as exc:
            reset_error = exc
    after = snapshot(repo)
    delta = changed_paths(repo, before, after)
    blobs = dict(before.get("blobs") or {})
    prefixes = [posix(p).rstrip("/") for p in skip_prefixes if p]
    # Checkout HEAD only when HEAD is the pre-hop commit. A failed reset
    # leaves the hostile commit; checking it out would write pwned bytes.
    allow_checkout = (not head_moved) or head(repo) == before_head
    for rel in delta:
        norm = posix(rel)
        if any(norm == p or norm.startswith(p + "/") for p in prefixes):
            continue
        _revert_one(repo, norm, blobs, allow_checkout=allow_checkout)
    if reset_error is not None:
        raise reset_error


def _path_in_head(repo: Path, rel: str) -> bool:
    """Whether HEAD's tree names this path. The live index is not this fact."""
    if not rel:
        return False
    out = git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", rel, check=False)
    want = posix(rel)
    return any(posix(line.strip()) == want for line in out.splitlines())


def _revert_one(
    repo: Path,
    rel: str,
    blobs: Dict[str, Optional[bytes]],
    *,
    allow_checkout: bool = True,
) -> None:
    path = repo / rel
    if rel in blobs:
        data = blobs[rel]
        if data is None:
            if path.is_file() or path.is_symlink():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return
    # Pre-hop identity is HEAD (or a blob), not the post-hop index. A git mv
    # drops the source from ls-files and stages a dest HEAD never had.
    if allow_checkout and is_git_repo(repo) and _path_in_head(repo, rel):
        git(repo, "checkout", "HEAD", "--", rel, check=True)
        return
    if not allow_checkout and is_git_repo(repo):
        # Tracked at a hostile commit. Leave the file; caller raises reset.
        return
    if path.is_file() or path.is_symlink():
        path.unlink()


def delta_paths(before: Sequence[str], after: Sequence[str]) -> List[str]:
    prior = set(before)
    return sorted(p for p in after if p not in prior)


def product_paths(paths: Iterable[str]) -> List[str]:
    """Fence paths that are not orchestrator work files."""
    out = []
    for path in paths:
        rel = posix(path)
        if not rel or rel.startswith(".team/") or escapes_repo(rel):
            continue
        out.append(rel)
    return out


def ignored_untracked(repo: Path) -> Set[str]:
    """The set ``porcelain_paths`` folds in beyond git's own dirty list.

    Fence membership needs these: a hop that writes a gitignored build file has
    still written product, and restore must be able to put it back. Reading is
    the other direction — a generated tree is not review material, and dumping
    it costs every downstream hop its full byte count.
    """
    out = git(repo, "ls-files", "-o", "-i", "--exclude-standard", check=False)
    return {
        rel
        for rel in (_unquote_git_path(line) for line in out.splitlines())
        if rel
    }


def worktree_diff(repo: Path, paths: Sequence[str]) -> str:
    """Unified diff of product paths vs HEAD, including new untracked files.

    Gitignored-untracked paths are fence members but not diff material; see
    ``ignored_untracked``.
    """
    chunks: List[str] = []
    skip = ignored_untracked(repo) if is_git_repo(repo) else set()
    for rel in product_paths(paths):
        if rel in skip:
            continue
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
        # name-status (not name-only): rename/copy detection lists both
        # sides; paths_from_status_line keeps the source of R and the dest
        # of C. name-only would drop the rename source.
        out = git(repo, "diff", "--name-status", before_head, after_head, check=False)
        for line in out.splitlines():
            found.update(paths_from_status_line(line))
    return sorted(found)


def submodule_paths(repo: Path) -> List[str]:
    """Submodule directories from ``.gitmodules`` and index gitlinks (mode 160000)."""
    found = set()
    if (repo / ".gitmodules").is_file():
        out = git(
            repo,
            "config",
            "-f",
            ".gitmodules",
            "--get-regexp",
            r"^submodule\..*\.path$",
            check=False,
        )
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                rel = posix(parts[1].strip()).rstrip("/")
                if rel:
                    found.add(rel)
    listed = git(repo, "ls-files", "-s", check=False)
    for line in listed.splitlines():
        if not line.startswith("160000 "):
            continue
        parts = line.split(None, 3)
        if len(parts) == 4:
            rel = posix(parts[3].strip()).rstrip("/")
            if rel:
                found.add(rel)
    return sorted(found)


def verify_delta(
    paths: Iterable[str],
    allowed_roots: Sequence[str],
    *,
    always_allowed: Sequence[str] = (),
    denied_roots: Sequence[str] = (),
) -> Tuple[List[str], List[str]]:
    """Return (ok, bad). Empty allowed_roots means advisory-only (all ok).

    denied_roots lose to always_allowed and win over allowed_roots.
    """
    ok: List[str] = []
    bad: List[str] = []
    always = [normalize_root(r) for r in always_allowed if normalize_root(r)]
    denied = [normalize_root(r) for r in denied_roots if normalize_root(r)]
    roots = [normalize_root(r) for r in allowed_roots if normalize_root(r)]
    for path in paths:
        if any(under_root(path, root) for root in always):
            ok.append(path)
            continue
        if any(under_root(path, root) for root in denied):
            bad.append(path)
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

    Informational only: a dirty tree is legal. The write fence is role roots,
    not pre-existing WIP. Listed so a verify report can show mixed edits.
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
    denied_roots: Sequence[str] = (),
) -> str:
    lines = [
        "phase: %s" % phase,
        "allowed_roots: %s" % (", ".join(allowed) if allowed else "(none — advisory)"),
    ]
    if denied_roots:
        lines.append("denied_roots: %s" % ", ".join(denied_roots))
    lines.append("delta (%d):" % len(delta))
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
    """Cumulative patch for the range. Every path appears exactly once.

    ``log -p --root`` re-emits a file's full hunk once per touching commit, so
    a branch that rewrites one module ten times pays for it ten times in every
    reader's context. The empty-tree diff is the same information, deduped.
    Rename *old* paths that only exist mid-branch are recovered by
    ``range_name_only`` from the log, not by duplicating the patch.
    """
    if base:
        return git(repo, "diff", "-M", "%s..HEAD" % base, check=False)
    return git(repo, "diff", "-M", EMPTY_TREE, "HEAD", check=False)


def paths_from_diff(patch: str) -> List[str]:
    found = set()
    for a, b in _DIFF_GIT.findall(patch or ""):
        for side in (a, b):
            rel = posix(side.strip())
            if rel and rel != "/dev/null":
                found.add(rel)
    return sorted(found)


def range_name_only(repo: Path, base: str) -> List[str]:
    """Every path the range touched, including ones that no longer exist.

    The cumulative patch only names surviving paths. A file created and then
    renamed or deleted mid-range is still part of the commit set, so the log
    supplies those names instead of the patch carrying duplicate hunks.
    """
    names = set(paths_from_diff(range_diff(repo, base)))
    spec = "%s..HEAD" % base if base else "HEAD"
    log = git(repo, "log", "--name-only", "--format=", "-M", spec, check=False)
    for line in log.splitlines():
        rel = posix(_unquote_git_path(line).strip())
        if rel and rel != "/dev/null":
            names.add(rel)
    return sorted(names)


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


def pr_commits_oneline(view_stdout: str) -> str:
    """`gh pr view --json title,commits` → git-log --oneline text.

    Compact JSON is one line; the commit set is the ``commits`` array.
    Never return the JSON document as a log.
    """
    try:
        data = json.loads(view_stdout or "")
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    lines: List[str] = []
    for item in data.get("commits") or []:
        if not isinstance(item, dict):
            continue
        oid = str(item.get("oid") or item.get("sha") or "").strip()
        if not oid:
            continue
        msg = str(item.get("messageHeadline") or item.get("message") or "").strip()
        short = oid[:12] if len(oid) > 12 else oid
        lines.append("%s %s" % (short, msg) if msg else short)
    return "\n".join(lines) + ("\n" if lines else "")


def oneline_commit_count(log: str) -> int:
    """Commits in a git-log --oneline (or equivalent) listing. Not JSON lines."""
    n = 0
    for line in (log or "").splitlines():
        text = line.strip()
        if not text or text.startswith("("):
            continue
        n += 1
    return n


def pr_bundle(repo: Path, pr: str) -> Tuple[str, str, str]:
    """Return (log, diff, how). Prefer gh; else merge-base with main/master.

    ``log`` is always a commit list (oneline), never gh JSON.
    ``how`` is ``gh`` only for the pair (non-empty oneline list, non-empty gh
    diff). A successful ``gh pr diff`` is not that claim.
    """
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
        log = pr_commits_oneline(view.stdout)
    if (
        gh is not None
        and gh.returncode == 0
        and (gh.stdout or "").strip()
        and oneline_commit_count(log) > 0
    ):
        return log, gh.stdout, "gh"
    for branch in ("main", "master", "origin/main", "origin/master"):
        mb = git(repo, "merge-base", branch, "HEAD", check=False).strip()
        if not mb or "fatal" in mb.lower():
            continue
        log = git(repo, "log", "--oneline", "%s..HEAD" % mb, check=False)
        # Same rule as the gh path above: a base is only usable if it leaves a
        # commit list behind. When HEAD *is* the base branch the merge-base is
        # HEAD, so `base..HEAD` is empty -- handing the reviewer an empty range
        # and a log that says "(empty range)". Keep looking, then fall back to
        # the branch, which at least names the commits under review.
        if oneline_commit_count(log) <= 0:
            continue
        return (
            log,
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
