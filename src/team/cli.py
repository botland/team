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
    apply_range_reviewer,
    default_roles,
    load_config,
    resolve_phase,
)
from team.pipeline import (
    OptionalPhaseError,
    PipelineError,
    load_pipeline,
    start_audit,
    start_feature,
    start_range_review,
)
from team import findings as findings_mod
from team import gitutil
from team.state import State, require_work, work_dir
from team.util import as_str, engine_root, load_json, slugify, write_text


def main(argv: Optional[List[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (PipelineError, findings_mod.FindingsError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="team",
        description="Split-team orchestrator: files as protocol, roles assigned to runtimes.",
        epilog=(
            "Each command has -h/--help, e.g. team review --help\n"
            "Global flags (--repo, --assign, --fake, --skip) go BEFORE the command:\n"
            "  team --assign reviewer=claude review\n"
            "  team --assign all=grok resume review-since-tag\n"
            "Past-commits reviewer can also be set on the command:\n"
            "  team review --reviewer claude"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--repo", default=None, help="Target git repo (default: cwd)")
    p.add_argument(
        "--assign",
        action="append",
        default=[],
        metavar="ROLE=RUNTIME",
        help=(
            "Override role runtime (repeatable). "
            "ROLE=claude|grok|both|host, or all=grok / all=claude "
            "(every role that accepts that runtime). "
            "Later flags win, e.g. --assign all=grok --assign architect=claude."
        ),
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
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    f = sub.add_parser(
        "feature",
        help="Start a feature pipeline",
        description="Architect → critic → TDD → implement → test → review.",
    )
    f.add_argument("brief", nargs=argparse.REMAINDER, help="Feature brief")
    f.add_argument("--slug", default="", help="Work slug (default: derived from brief)")
    f.add_argument("--dry-run", action="store_true", help="Stop after TDD design; no test/prod writes")
    f.add_argument("--stop-after", default="", help="Stop after this phase")
    f.add_argument("--force", action="store_true", help="Replace an existing work dir")
    f.set_defaults(func=cmd_feature)

    r = sub.add_parser(
        "resume",
        help="Continue an existing run",
        description="Resume .team/work/<slug> from the next unfinished phase, or --from PHASE.",
    )
    r.add_argument("slug")
    r.add_argument("--from", dest="from_phase", default="", help="Restart from this phase")
    r.add_argument("--stop-after", default="")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_resume)

    v = sub.add_parser(
        "review",
        help="Review a work slug, or commits since the last reviewed-* tag (not only PRs)",
        description=(
            "Three scopes:\n"
            "  team review <slug>     re-review a feature/audit run (uses that run's reviewer)\n"
            "  team review --pr N     PR review: Claude and Grok (both)\n"
            "  team review            past commits since last reviewed-* tag: one reviewer\n"
            "\n"
            "Past-commits default is grok. Force Claude with:\n"
            "  team review --reviewer claude\n"
            "  team --assign reviewer=claude review\n"
            "Config: [review] range_reviewer = \"claude\"\n"
            "reviewer=both is rejected on past-commits."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    v.add_argument("--force", action="store_true", help="Replace an existing range-review work dir")
    v.add_argument(
        "--reviewer",
        choices=("claude", "grok"),
        default=None,
        help="Force the past-commits reviewer (default: grok). Also allowed with --pr.",
    )
    v.add_argument(
        "--list-tags",
        action="store_true",
        help="List reviewed-* tags and which one is the next past-commits base",
    )
    v.add_argument(
        "--show-range",
        action="store_true",
        help="Print the pending past-commits (or --pr) range without reviewing",
    )
    v.add_argument(
        "--mark",
        nargs="?",
        const="HEAD",
        default=None,
        metavar="REF",
        help="Create a reviewed-* tag at REF (default HEAD) without reviewing",
    )
    v.add_argument(
        "--delete-tag",
        default="",
        metavar="TAG",
        help="Delete a reviewed-* tag (does not review)",
    )
    v.set_defaults(func=cmd_review)

    p_ap = sub.add_parser(
        "apply",
        help="Apply classified review findings (re-review if kind= is missing)",
        description=(
            "Route review findings by kind: architecture → replan, test → contract+tests, "
            "implementation → production. Unstructured findings trigger a re-review first."
        ),
    )
    p_ap.add_argument("slug")
    p_ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify and write apply-plan.md; do not edit the repo",
    )
    p_ap.add_argument(
        "--no-review",
        action="store_true",
        help="Do not run a closing review after applying",
    )
    p_ap.set_defaults(func=cmd_apply)

    p_re = sub.add_parser(
        "replan",
        help="Architect writes a design delta",
        description="Read review.md and write design-replan.md. --continue applies it and resumes TDD.",
    )
    p_re.add_argument("slug")
    p_re.add_argument(
        "--continue",
        dest="do_continue",
        action="store_true",
        help="Apply design-replan.md as design.md and resume from TDD design",
    )
    p_re.set_defaults(func=cmd_replan)

    lst = sub.add_parser(
        "list",
        help="List .team/work runs in the target repo",
        description="Print slug, mode, phase, and stop reason for each run.",
    )
    lst.set_defaults(func=cmd_list)

    s = sub.add_parser(
        "status",
        help="Print phase rail from state.json (no model)",
        description="Show done/skip/pending phases and artifact paths for a slug.",
    )
    s.add_argument("slug")
    s.set_defaults(func=cmd_status)

    roles = sub.add_parser(
        "roles",
        help="Show default and resolved role assignment",
        description="Print each role's runtime and the past-commits range_reviewer default.",
    )
    roles.set_defaults(func=cmd_roles)

    init = sub.add_parser(
        "init",
        help="Write .team/config.toml in the target repo",
        description="Copy config.example.toml to <repo>/.team/config.toml and ignore work/.",
    )
    init.set_defaults(func=cmd_init)

    audit = sub.add_parser(
        "audit",
        help="Read-only status-audit: scout → assess → review (no implementation)",
        description="Read-only. Writes report.md (status + review). First leftover token may be a repo path.",
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
    if args.list_tags:
        return _cmd_review_list_tags(repo)
    if args.delete_tag:
        return _cmd_review_delete_tag(repo, args.delete_tag)
    if args.mark is not None:
        return _cmd_review_mark(repo, args.mark)
    if args.show_range:
        return _cmd_review_show_range(args, repo)

    slug = args.slug or ""
    range_requested = bool(args.pr or args.since or not slug)
    work_exists = bool(slug) and (work_dir(repo, slug) / "state.json").is_file()
    if work_exists and not args.pr and not args.since:
        cfg = _cfg(args)
        if args.reviewer:
            cfg.roles["reviewer"] = args.reviewer
            cfg.role_overrides.add("reviewer")
        pipe = load_pipeline(cfg, slug)
        print("== team review %s" % slug)
        pipe.phase_reviewer()
        pipe.state.mark("reviewer")
        if pipe.state.mode != "audit" and "guardian" not in cfg.skip:
            try:
                pipe.phase_guardian()
                pipe.state.mark("guardian")
            except OptionalPhaseError as exc:
                pipe._skip("guardian", str(exc))
        pipe.save()
        _print_done(pipe)
        return 0
    if slug and not range_requested:
        print("No run at %s (missing state.json)" % work_dir(repo, slug), file=sys.stderr)
        return 1
    cfg = _cfg(args, force=args.force)
    try:
        apply_range_reviewer(cfg, pr=bool(args.pr), reviewer=args.reviewer or "")
    except SystemExit as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    stamp = bool(args.stamp) or (bool(args.pr) and not args.no_stamp)
    if args.no_stamp:
        stamp = False
    if args.pr:
        slug = slug or ("review-pr-%s" % args.pr)
        desc = "PR %s" % args.pr
        who = cfg.roles.get("reviewer") or "both"
    else:
        slug = slug or "review-since-tag"
        desc = "commits since last dedicated tag"
        who = cfg.roles.get("reviewer") or cfg.range_reviewer
    print("== team review %s" % slug)
    print("repo: %s" % cfg.repo)
    print("scope: %s" % desc)
    print("reviewer: %s" % who)
    pipe = start_range_review(cfg, slug=slug, pr=args.pr, since=args.since)
    print("range: %s" % pipe.state.brief)
    pipe.run()
    if stamp:
        rc = _stamp_review(pipe, explicit=bool(args.stamp) and not args.no_stamp)
        if rc:
            _print_done(pipe)
            return rc
    _print_done(pipe)
    return 0


def _stamp_review(pipe, *, explicit: bool) -> int:
    reasons = []
    if pipe.state.stop_reason != "complete":
        reasons.append("stop_reason=%s" % (pipe.state.stop_reason or "unset"))

    assignment = pipe.cfg.assignment("reviewer")
    if assignment == "both":
        expected = ["reviewer-claude.result.json", "reviewer-grok.result.json"]
    else:
        expected = ["reviewer-%s.result.json" % assignment]
    findings_count = 0
    runtimes = []
    for name in expected:
        path = pipe.work / "prompts" / name
        if not path.is_file():
            reasons.append("missing %s" % name)
            continue
        try:
            data = load_json(path)
        except Exception:
            reasons.append("%s is not JSON" % name)
            continue
        if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
            reasons.append("%s has no findings array" % name)
            continue
        findings_count += len(data.get("findings") or [])
        runtimes.append(name.replace("reviewer-", "").replace(".result.json", ""))

    collected = ""
    git_state = pipe.state.git if isinstance(pipe.state.git, dict) else {}
    start = git_state.get("start") if isinstance(git_state.get("start"), dict) else {}
    collected = as_str(start.get("head")) or as_str(git_state.get("head"))
    now = gitutil.head(pipe.repo)
    if collected and now and collected != now:
        reasons.append("HEAD moved since range collection")

    skipped = "guardian" in pipe.cfg.skip or "guardian" in (pipe.state.skipped or [])
    guardian_path = pipe.work / "prompts" / "guardian.result.json"
    if skipped:
        print("warning: guardian skipped", file=sys.stderr)
    elif not guardian_path.is_file():
        reasons.append("guardian absent")

    if pipe.state.range_pr:
        oid, how = gitutil.pr_head_oid(pipe.repo, pipe.state.range_pr)
        source = pipe.state.range_source or as_str(git_state.get("range_how"))
        if how != "gh" or not oid:
            reasons.append("pr stamp requires gh headRefOid (got %s)" % (how or source or "fallback"))
        elif collected and oid != collected:
            reasons.append("pr headRefOid does not match collected HEAD")

    dirty = gitutil.porcelain_paths(pipe.repo) if gitutil.is_git_repo(pipe.repo) else []
    if dirty:
        print("warning: dirty working tree (%d paths)" % len(dirty), file=sys.stderr)

    if reasons:
        print("stamp refused: %s" % "; ".join(reasons), file=sys.stderr)
        return 1 if explicit else 0

    head_sha = collected or now
    guardian_status = "skipped" if skipped else "ok"
    message = "\n".join(
        [
            "slug=%s" % pipe.state.slug,
            "reviewed-head=%s" % head_sha,
            "reviewer=%s" % ",".join(runtimes) or assignment,
            "findings=%d" % findings_count,
            "guardian=%s" % guardian_status,
            "range-base=%s" % (pipe.state.range_base or "(root)"),
        ]
    )
    try:
        tag = gitutil.stamp_reviewed(pipe.repo, head_sha or "HEAD", message=message)
    except gitutil.GitError as exc:
        print("stamp refused: %s" % exc, file=sys.stderr)
        return 1 if explicit else 0
    pipe.state.stamp_tag = tag
    pipe.save()
    print("tag: %s" % tag)
    return 0


def _cmd_review_list_tags(repo: Path) -> int:
    if not gitutil.is_git_repo(repo):
        print("error: %s is not a git repository" % repo, file=sys.stderr)
        return 1
    rows = gitutil.list_reviewed_tags(repo)
    base, kind = gitutil.resolve_review_base(repo)
    print("next past-commits base: %s (%s)" % (base or "(branch root)", kind))
    print("commits pending: %d" % gitutil.commit_count(repo, base))
    print("")
    if not rows:
        print("no reviewed-* tags")
        return 0
    print("%-28s %-10s %-12s %s" % ("TAG", "COMMIT", "AHEAD", "FLAGS"))
    for row in rows:
        flags = []
        if row.get("current"):
            flags.append("next-base")
        if not row.get("ancestor"):
            flags.append("not-ancestor")
        print(
            "%-28s %-10s %-12s %s"
            % (
                row["tag"],
                row["commit"],
                row["commits_ahead"],
                ",".join(flags) or "-",
            )
        )
    return 0


def _cmd_review_delete_tag(repo: Path, name: str) -> int:
    if not gitutil.is_git_repo(repo):
        print("error: %s is not a git repository" % repo, file=sys.stderr)
        return 1
    try:
        tag = gitutil.delete_reviewed_tag(repo, name)
    except gitutil.GitError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("deleted %s" % tag)
    base, kind = gitutil.resolve_review_base(repo)
    print("next past-commits base: %s (%s)" % (base or "(branch root)", kind))
    print("commits pending: %d" % gitutil.commit_count(repo, base))
    return 0


def _cmd_review_mark(repo: Path, ref: str) -> int:
    if not gitutil.is_git_repo(repo):
        print("error: %s is not a git repository" % repo, file=sys.stderr)
        return 1
    try:
        commit = gitutil.resolve_commit(repo, ref)
        tag = gitutil.stamp_reviewed(repo, ref)
    except gitutil.GitError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("tag: %s" % tag)
    print("ref: %s (%s)" % (ref, commit[:12]))
    if not gitutil.is_ancestor(repo, tag):
        print(
            "warning: %s is not an ancestor of HEAD; next unscoped review will ignore it"
            % tag,
            file=sys.stderr,
        )
    base, kind = gitutil.resolve_review_base(repo)
    print("next past-commits base: %s (%s)" % (base or "(branch root)", kind))
    print("commits pending: %d" % gitutil.commit_count(repo, base))
    return 0


def _cmd_review_show_range(args, repo: Path) -> int:
    if not gitutil.is_git_repo(repo):
        print("error: %s is not a git repository" % repo, file=sys.stderr)
        return 1
    cfg = _cfg(args)
    try:
        apply_range_reviewer(cfg, pr=bool(args.pr), reviewer=args.reviewer or "")
    except SystemExit as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.pr:
        log, _diff, how = gitutil.pr_bundle(repo, args.pr)
        count = len([ln for ln in log.splitlines() if ln.strip()])
        desc = gitutil.describe_range(args.pr, "pr", count) + " [%s]" % how
        print("kind: pr")
        print("pr: %s" % args.pr)
        print("how: %s" % how)
        print("reviewer: %s" % (cfg.roles.get("reviewer") or "both"))
        print("commits: %d" % count)
        print("scope: %s" % desc)
    else:
        base, kind = gitutil.resolve_review_base(repo, args.since or "")
        log = gitutil.range_log(repo, base)
        count = gitutil.commit_count(repo, base)
        desc = gitutil.describe_range(base, kind, count)
        print("kind: %s" % kind)
        print("base: %s" % (base or "(root)"))
        print("reviewer: %s" % (cfg.roles.get("reviewer") or cfg.range_reviewer))
        print("commits: %d" % count)
        print("head: %s" % (gitutil.head(repo)[:12]))
        print("scope: %s" % desc)
    if (log or "").strip():
        print("")
        print(log.rstrip())
    return 0


def cmd_apply(args) -> int:
    cfg = _cfg(args, dry_run=args.dry_run)
    pipe = load_pipeline(cfg, args.slug)
    print("== team apply %s" % args.slug)
    print("work: %s" % pipe.work)
    pipe.apply_review(dry_run=args.dry_run, rereview=not args.no_review)
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
    apply_summary = work / "apply-summary.md"
    if apply_summary.is_file():
        print("apply: %s" % apply_summary)
    return 0


def cmd_roles(args) -> int:
    cfg = _cfg(args)
    print("engine: %s" % engine_root())
    print("repo:   %s" % cfg.repo)
    print("")
    print("%-14s %-10s %s" % ("ROLE", "ASSIGNED", "ALLOWED"))
    for role, spec in ROLES.items():
        print("%-14s %-10s %s" % (role, cfg.assignment(role), ", ".join(spec["runtimes"])))
    print("")
    print("range_reviewer %s  (past-commits; one runtime)" % cfg.range_reviewer)
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
    apply_summary = pipe.work / "apply-summary.md"
    if apply_summary.is_file():
        print("apply: %s" % apply_summary)
