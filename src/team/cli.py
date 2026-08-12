from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from team.config import (
    AUDIT_PHASE_ORDER,
    DEFAULT_AUDIT_QUERY,
    PHASE_ORDER,
    RANGE_PHASE_ORDER,
    ROLES,
    default_roles,
    load_config,
    resolve_phase,
)
from team.pipeline import (
    PipelineError,
    load_pipeline,
    start_audit,
    start_feature,
    start_range_review,
)
from team import gitutil
from team.state import State, require_work, work_dir
from team.util import engine_root, slugify, write_text


def main(argv: Optional[List[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PipelineError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="team",
        description="Split-team orchestrator: files as protocol, roles assigned to runtimes.",
    )
    p.add_argument("--repo", default=None, help="Target git repo (default: cwd)")
    p.add_argument(
        "--assign",
        action="append",
        default=[],
        metavar="ROLE=RUNTIME",
        help="Override role runtime (repeatable). Runtimes: claude, grok, both, host.",
    )
    p.add_argument(
        "--skip",
        action="append",
        default=[],
        help="Skip optional phases (critic, adversarial, guardian, debugger).",
    )
    p.add_argument("--fake", action="store_true", help="Do not call Claude/Grok; emit canned artifacts.")
    p.add_argument("--code-root", default="", help="Override implementation root")
    p.add_argument("--test-root", default="", help="Override test root")
    p.add_argument("--test-command", default="", help="Override test command")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("feature", help="Start a feature pipeline")
    f.add_argument("brief", nargs=argparse.REMAINDER, help="Feature brief")
    f.add_argument("--slug", default="", help="Work slug (default: derived from brief)")
    f.add_argument("--dry-run", action="store_true", help="Stop after TDD design; no test/prod writes")
    f.add_argument("--stop-after", default="", help="Stop after this phase")
    f.add_argument("--force", action="store_true", help="Replace an existing work dir")
    f.set_defaults(func=cmd_feature)

    r = sub.add_parser("resume", help="Continue an existing run")
    r.add_argument("slug")
    r.add_argument("--from", dest="from_phase", default="", help="Restart from this phase")
    r.add_argument("--stop-after", default="")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_resume)

    v = sub.add_parser(
        "review",
        help="Review a work slug, or commits since the last reviewed-* tag (not only PRs)",
    )
    v.add_argument(
        "slug",
        nargs="?",
        default="",
        help="Existing .team/work/<slug> to re-review. Omit for a commit-range review.",
    )
    v.add_argument("--pr", default="", help="Review this PR number (gh pr diff, else merge-base)")
    v.add_argument("--since", default="", help="Review commits since this tag/ref (overrides last dedicated tag)")
    v.add_argument(
        "--stamp",
        action="store_true",
        help="Create a reviewed-YYYYMMDD-HHMM tag after the review (vibe.rc gittag)",
    )
    v.add_argument("--no-stamp", action="store_true", help="Do not create a reviewed-* tag")
    v.add_argument("--force", action="store_true")
    v.set_defaults(func=cmd_review)

    p_re = sub.add_parser("replan", help="Architect writes a design delta")
    p_re.add_argument("slug")
    p_re.add_argument(
        "--continue",
        dest="do_continue",
        action="store_true",
        help="Apply design-replan.md as design.md and resume from TDD design",
    )
    p_re.set_defaults(func=cmd_replan)

    lst = sub.add_parser("list", help="List .team/work runs in the target repo")
    lst.set_defaults(func=cmd_list)

    s = sub.add_parser("status", help="Print phase rail from state.json (no model)")
    s.add_argument("slug")
    s.set_defaults(func=cmd_status)

    roles = sub.add_parser("roles", help="Show default and resolved role assignment")
    roles.set_defaults(func=cmd_roles)

    init = sub.add_parser("init", help="Write .team/config.toml in the target repo")
    init.set_defaults(func=cmd_init)

    audit = sub.add_parser(
        "audit",
        help="Read-only status-audit: scout → assess → review (no implementation)",
    )
    audit.add_argument(
        "--depth",
        default="medium",
        help="quick | medium | thorough (default: medium)",
    )
    audit.add_argument("--slug", default="", help="Work slug (default: derived from query)")
    audit.add_argument("--force", action="store_true")
    audit.add_argument("--dry-run", action="store_true", help="Stop after assess; skip review")
    audit.add_argument("--stop-after", default="", help="Stop after this audit phase")
    audit.add_argument(
        "rest",
        nargs=argparse.REMAINDER,
        help="[repo] question...  First token is repo if it is an existing directory.",
    )
    audit.set_defaults(func=cmd_audit)
    return p


def _cfg(args, **kwargs):
    repo = Path(args.repo).resolve() if args.repo else Path.cwd()
    return load_config(
        repo,
        assign=args.assign,
        skip=args.skip,
        fake=args.fake,
        code_root=args.code_root,
        test_root=args.test_root,
        test_command=args.test_command,
        depth=getattr(args, "depth", "") or "",
        **kwargs,
    )


def cmd_feature(args) -> int:
    brief = " ".join(args.brief).strip()
    if any(tok.startswith("--") for tok in (args.brief or [])):
        print(
            "Put flags before the brief, e.g.: team feature --dry-run Add a greet helper",
            file=sys.stderr,
        )
        return 2
    if not brief:
        print("Need a feature brief, e.g.: team feature Add a greet helper", file=sys.stderr)
        return 2
    cfg = _cfg(
        args,
        dry_run=args.dry_run,
        force=args.force,
        stop_after=args.stop_after or None,
    )
    slug = args.slug or slugify(brief)
    print("== team feature %s" % slug)
    print("repo: %s" % cfg.repo)
    print("work: %s" % work_dir(cfg.repo, slug))
    print("assign: %s" % _fmt_roles({k: cfg.assignment(k) for k in cfg.roles}))
    pipe = start_feature(cfg, brief, slug)
    pipe.run()
    _print_done(pipe)
    return 0


def cmd_resume(args) -> int:
    cfg = _cfg(args, dry_run=args.dry_run, stop_after=args.stop_after or None)
    pipe = load_pipeline(cfg, args.slug)
    start = resolve_phase(args.from_phase) if args.from_phase else None
    print("== team resume %s" % args.slug)
    print("work: %s" % pipe.work)
    pipe.run(start=start)
    _print_done(pipe)
    return 0


def cmd_review(args) -> int:
    repo = Path(args.repo).resolve() if args.repo else Path.cwd()
    slug = args.slug or ""
    range_requested = bool(args.pr or args.since or not slug)
    work_exists = bool(slug) and (work_dir(repo, slug) / "state.json").is_file()
    if work_exists and not args.pr and not args.since:
        cfg = _cfg(args)
        pipe = load_pipeline(cfg, slug)
        print("== team review %s" % slug)
        pipe.phase_reviewer()
        pipe.state.mark("reviewer")
        if pipe.state.mode != "audit" and "guardian" not in cfg.skip:
            pipe.phase_guardian()
            pipe.state.mark("guardian")
        pipe.save()
        _print_done(pipe)
        return 0
    if slug and not range_requested:
        print("No run at %s (missing state.json)" % work_dir(repo, slug), file=sys.stderr)
        return 1
    cfg = _cfg(args, force=args.force)
    stamp = bool(args.stamp) or (bool(args.pr) and not args.no_stamp)
    if args.no_stamp:
        stamp = False
    if args.pr:
        slug = slug or ("review-pr-%s" % args.pr)
        desc = "PR %s" % args.pr
    else:
        slug = slug or "review-since-tag"
        desc = "commits since last dedicated tag"
    print("== team review %s" % slug)
    print("repo: %s" % cfg.repo)
    print("scope: %s" % desc)
    pipe = start_range_review(cfg, slug=slug, pr=args.pr, since=args.since)
    print("range: %s" % pipe.state.brief)
    pipe.run()
    if stamp and pipe.state.stop_reason == "complete":
        try:
            tag = gitutil.stamp_reviewed(cfg.repo)
            pipe.state.stamp_tag = tag
            pipe.save()
            print("tag: %s" % tag)
        except gitutil.GitError as exc:
            print("stamp failed: %s" % exc, file=sys.stderr)
    _print_done(pipe)
    return 0


def cmd_replan(args) -> int:
    cfg = _cfg(args)
    pipe = load_pipeline(cfg, args.slug)
    print("== team replan %s" % args.slug)
    pipe.replan()
    print("wrote %s" % (pipe.work / "design-replan.md"))
    if args.do_continue:
        pipe.apply_replan()
        _print_done(pipe)
    return 0


def cmd_list(args) -> int:
    repo = Path(args.repo).resolve() if args.repo else Path.cwd()
    root = repo / ".team" / "work"
    if not root.is_dir():
        print("no runs in %s" % root)
        return 0
    print("%-28s %-8s %-18s %s" % ("SLUG", "MODE", "PHASE", "STOP"))
    found = False
    for child in sorted(root.iterdir()):
        if not (child / "state.json").is_file():
            continue
        found = True
        state = State.load(child)
        print(
            "%-28s %-8s %-18s %s"
            % (state.slug, state.mode, state.phase, state.stop_reason or "(in progress)")
        )
    if not found:
        print("no runs in %s" % root)
    return 0


def cmd_status(args) -> int:
    repo = Path(args.repo).resolve() if args.repo else Path.cwd()
    work = require_work(repo, args.slug)
    state = State.load(work)
    print("slug: %s" % state.slug)
    print("repo: %s" % state.repo)
    print("work: %s" % work)
    print("phase: %s" % state.phase)
    print("stop: %s" % (state.stop_reason or "(in progress)"))
    print("code_root: %s" % state.code_root)
    print("test_root: %s" % state.test_root)
    print("assign: %s" % _fmt_roles(state.assignment or default_roles()))
    print("")
    if state.mode == "audit":
        order = AUDIT_PHASE_ORDER
    elif state.mode == "range":
        order = RANGE_PHASE_ORDER
    else:
        order = PHASE_ORDER
    done = set(state.phases_done)
    skipped = set(state.skipped)
    print("mode: %s" % state.mode)
    for phase in order:
        if phase in skipped:
            mark = "skip"
        elif phase in done:
            mark = "done"
        else:
            mark = "...."
        print("  [%s] %s" % (mark, phase))
    review = work / "review.md"
    if review.is_file():
        print("")
        print("review: %s" % review)
    report = work / "report.md"
    if report.is_file():
        print("report: %s" % report)
    return 0


def cmd_roles(args) -> int:
    cfg = _cfg(args)
    print("engine: %s" % engine_root())
    print("repo:   %s" % cfg.repo)
    print("")
    print("%-14s %-10s %s" % ("ROLE", "ASSIGNED", "ALLOWED"))
    for role, spec in ROLES.items():
        print("%-14s %-10s %s" % (role, cfg.assignment(role), ", ".join(spec["runtimes"])))
    return 0


def cmd_init(args) -> int:
    repo = Path(args.repo).resolve() if args.repo else Path.cwd()
    dest = repo / ".team" / "config.toml"
    if dest.exists():
        print("already exists: %s" % dest)
        return 0
    src = engine_root() / "config.example.toml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    write_text(repo / ".team" / ".gitignore", "work/\n")
    print("wrote %s" % dest)
    return 0


def cmd_audit(args) -> int:
    rest = list(args.rest or [])
    repo_flag = Path(args.repo).resolve() if args.repo else None
    leftover_repo = None
    if rest:
        candidate = Path(rest[0]).expanduser()
        if candidate.exists() and candidate.is_dir():
            leftover_repo = candidate.resolve()
            rest = rest[1:]
    repo = leftover_repo or repo_flag or Path.cwd()
    query = " ".join(rest).strip() or DEFAULT_AUDIT_QUERY
    # Re-bind --repo so _cfg sees the leftover directory.
    args.repo = str(repo)
    cfg = _cfg(
        args,
        force=args.force,
        dry_run=args.dry_run,
        stop_after=args.stop_after or None,
    )
    slug = args.slug or ("audit" if query == DEFAULT_AUDIT_QUERY else "audit-" + slugify(query))
    print("== team audit %s" % slug)
    print("repo: %s" % cfg.repo)
    print("query: %s" % query)
    print("depth: %s" % cfg.depth)
    print("work: %s" % work_dir(cfg.repo, slug))
    print("assign: %s" % _fmt_roles({k: cfg.assignment(k) for k in cfg.roles}))
    pipe = start_audit(cfg, query, slug)
    pipe.run()
    _print_done(pipe)
    return 0


def _fmt_roles(roles: dict) -> str:
    return " ".join("%s=%s" % (k, v) for k, v in roles.items())


def _print_done(pipe) -> None:
    print("work: %s" % pipe.work)
    print("stop: %s" % (pipe.state.stop_reason or pipe.state.phase))
    review = pipe.work / "review.md"
    if review.is_file():
        print("review: %s" % review)
    report = pipe.work / "report.md"
    if report.is_file():
        print("report: %s" % report)
