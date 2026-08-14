from __future__ import annotations

import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from team import findings as findings_mod
from team import gitutil, style, testhost
from team.config import (
    AUDIT_PHASE_ORDER,
    PHASE_ORDER,
    RANGE_PHASE_ORDER,
    ROLES,
    Config,
    persona_path,
    schema_path,
)
from team.merge import merge_reviews
from team.runners import (
    Result,
    Runtime,
    describe_runtime_failure,
    premature_inspect,
    resolve_session,
    runtime_for,
)
from team.schemas import validate as validate_schema
from team.state import State, work_dir
from team.util import (
    as_bool,
    as_list,
    as_str,
    dump_json,
    engine_root,
    explicit_roots,
    load_json,
    write_text,
)


class PipelineError(RuntimeError):
    pass


class OptionalPhaseError(PipelineError):
    """Optional role (guardian, critic, …) could not run. Skip, do not abort."""


class Pipeline:
    def __init__(self, cfg: Config, state: State, work: Path) -> None:
        self.cfg = cfg
        self.state = state
        self.work = work
        self.repo = Path(state.repo)
        self.log_lines: List[str] = []

    def log(self, msg: str) -> None:
        self.log_lines.append(msg)
        print(msg, flush=True)

    def _begin_hop(self, role: str, label: str) -> None:
        """One start line before a long hop. Completion stays on the existing log."""
        if role in ROLES:
            who = self.cfg.assignment(role)
        else:
            who = role
        self.log("%s  %s (%s) …" % (datetime.now().strftime("%H:%M:%S"), label, who))

    def _log_items(
        self,
        items: List[Dict[str, Any]],
        *,
        sort: bool = False,
        more_hint: str = "",
    ) -> None:
        """After a role exits: at most 10 important items. No-op if none."""
        for line in findings_mod.format_console_lines(
            items, sort=sort, more_hint=more_hint
        ):
            self.log(line)

    def save(self) -> None:
        self.state.code_root = self.cfg.code_root
        self.state.test_root = self.cfg.test_root
        self.state.test_command = self.cfg.test_command
        self.state.save(self.work)

    def artifact(self, name: str) -> Path:
        return self.work / name

    def read_artifact(self, name: str) -> str:
        path = self.artifact(name)
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def write_artifact(self, name: str, content: str) -> Path:
        return write_text(self.artifact(name), content)

    def schema(self, name: str) -> Dict[str, Any]:
        return load_json(schema_path(name))

    def persona(self, role: str) -> str:
        path = persona_path(role)
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return "You are the %s." % role

    def should_skip(self, phase: str) -> bool:
        return phase in self.cfg.skip

    def done(self, phase: str) -> bool:
        return phase in self.state.phases_done

    def invoke(
        self,
        role: str,
        phase: str,
        prompt: str,
        schema_name: str,
        *,
        capability: Optional[str] = None,
        resume: bool = False,
        extra: Optional[Dict[str, Any]] = None,
        runtime_name: Optional[str] = None,
    ) -> Result:
        runtime_name = runtime_name or self.cfg.assignment(role)
        if runtime_name == "host":
            raise PipelineError("role %s is host-only and should not be invoked" % role)
        if runtime_name == "both":
            raise PipelineError("reviewer=both must pick a concrete runtime")
        cap = capability or ROLES[role]["capability"]
        self._require_write_scope(role, cap)
        rt: Runtime = runtime_for(runtime_name)
        session_key = "%s:%s" % (role, runtime_name)
        # Replan consults tdd-design, then apply invokes it again. Passing the
        # stored id as a *new* --session-id fails: "Session ID is already in use."
        sid, do_resume = resolve_session(self.state.sessions.get(session_key, ""))
        extra = dict(extra or {})
        extra.setdefault("code_root", self.cfg.code_root)
        extra.setdefault("test_root", self.cfg.test_root)
        if role in ("architect", "critic", "reviewer", "guardian", "tdd-design"):
            extra.setdefault("effort", "high")
        result = rt.complete(
            role=role,
            phase=phase,
            prompt=prompt,
            schema=self.schema(schema_name),
            capability=cap,
            session_id=sid,
            resume=do_resume,
            work=self.work,
            repo=self.repo,
            timeout=self.cfg.phase_timeout,
            extra=extra,
        )
        if result.session_id:
            self.state.sessions[session_key] = result.session_id
        output = dict(result.output) if isinstance(result.output, dict) else {"value": result.output}
        output["_meta"] = {
            "slug": self.state.slug,
            "attempt": (self.state.last_review or {}).get("attempt") or 0,
            "phase": phase,
            "role": role,
            "runtime": runtime_name,
            "head": gitutil.head(self.repo) if gitutil.is_git_repo(self.repo) else "",
            "range_base": self.state.range_base,
        }
        result_path = write_text(
            self.work / "prompts" / ("%s.result.json" % phase),
            json.dumps(output, indent=2)[:200000],
        )
        if str(phase) in ("reviewer-claude", "reviewer-grok", "reviewer-fake"):
            self._record_review_result(result_path)
        if not result.success:
            err = describe_runtime_failure(result)
            if ROLES.get(role, {}).get("optional"):
                raise OptionalPhaseError("%s: %s" % (phase, err))
            raise PipelineError("%s/%s failed: %s" % (role, phase, err))
        errors = validate_schema(result.output, self.schema(schema_name), enums=False)
        if errors:
            keys = ""
            if isinstance(result.output, dict) and result.output:
                keys = " (keys: %s)" % ", ".join(sorted(result.output))
            msg = "%s/%s failed: schema %s%s" % (
                role,
                phase,
                "; ".join(errors),
                keys,
            )
            if ROLES.get(role, {}).get("optional"):
                raise OptionalPhaseError(msg)
            raise PipelineError(msg)
        if premature_inspect(role=role, runtime=runtime_name, result=result):
            if extra.get("_inspect_retry"):
                msg = (
                    "%s/%s failed: finished in %s model turn(s) without inspecting the tree"
                    % (role, phase, result.num_turns)
                )
                if ROLES.get(role, {}).get("optional"):
                    raise OptionalPhaseError(msg)
                raise PipelineError(msg)
            extra["_inspect_retry"] = True
            self.log(
                "%s emitted schema JSON in %s turn(s); retrying so it inspects first"
                % (phase, result.num_turns)
            )
            return self.invoke(
                role,
                phase,
                prompt
                + "\n\nPREVIOUS OUTPUT REJECTED: it was emitted before any tool call. "
                "That is not a review. Read the listed artifacts with tools, then "
                "emit the final JSON only after you have inspected the tree.",
                schema_name,
                capability=capability,
                resume=True,
                extra=extra,
                runtime_name=runtime_name,
            )
        return result

    def _require_write_scope(self, role: str, capability: str) -> None:
        if capability == "write-code" and not explicit_roots(self.cfg.code_root):
            raise PipelineError(
                "%s: write capability requires an explicit code_root; "
                "set paths.code_root or use '.'" % role
            )
        if capability == "write-tests" and not explicit_roots(self.cfg.test_root):
            raise PipelineError(
                "%s: write capability requires an explicit test_root; "
                "set paths.test_root or use '.'" % role
            )

    def _begin_review_attempt(self) -> None:
        prev = self.state.last_review if isinstance(self.state.last_review, dict) else {}
        attempt = int(prev.get("attempt") or 0) + 1
        self.state.last_review = {"attempt": attempt, "results": []}
        self.save()

    def _record_review_result(self, path: Path) -> None:
        rec = self.state.last_review if isinstance(self.state.last_review, dict) else {}
        results = [row for row in as_list(rec.get("results")) if isinstance(row, dict)]
        results = [row for row in results if row.get("name") != path.name]
        results.append(
            {
                "name": path.name,
                "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        rec = dict(rec)
        rec["results"] = results
        rec.setdefault("attempt", 1)
        self.state.last_review = rec
        self.save()

    def _refresh_recorded_review_digests(self) -> None:
        rec = self.state.last_review if isinstance(self.state.last_review, dict) else {}
        rows = [row for row in as_list(rec.get("results")) if isinstance(row, dict)]
        prompts = self.work / "prompts"
        updated = []
        for row in rows:
            name = as_str(row.get("name"))
            path = prompts / name
            if not name or not path.is_file():
                continue
            updated.append(
                {
                    "name": name,
                    "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        rec = dict(rec)
        rec["results"] = updated
        rec.setdefault("attempt", 1)
        self.state.last_review = rec
        self.save()

    def consult(self, target: str, questions: List[str], from_role: str) -> str:
        target = {
            "tdd": "tdd-design",
            "tdd-dev": "tdd-design",
            "tdd-design": "tdd-design",
            "test-writer": "test-writer",
            "architect": "architect",
            "implementer": "implementer",
        }.get(target, target)
        if target not in ROLES:
            target = "architect"
        self._begin_hop(target, "apply: consult %s" % target)
        n = len(list(self.work.joinpath("consult").glob("*.json"))) + 1
        payload = {
            "from": from_role,
            "to": target,
            "questions": questions[:10],
        }
        write_text(
            self.work / "consult" / ("%03d-%s-%s.json" % (n, from_role, target)),
            json.dumps(payload, indent=2),
        )
        prompt = self._prompt(
            target,
            [
                "You are answering consult questions from the **%s**." % from_role,
                "Do not edit files.",
                "Questions (max 10):",
                json.dumps(questions[:10], indent=2),
                "Read the work artifacts and the repo, then answer.",
            ],
        )
        result = self.invoke(
            target,
            "consult-%03d" % n,
            prompt,
            "answers.json",
            capability="read-only",
            resume=True,
        )
        answers = as_str(result.output.get("answers_markdown")) or "(empty consult answers)"
        write_text(
            self.work / "consult" / ("%03d-%s-%s-answers.md" % (n, from_role, target)),
            answers,
        )
        return answers

    def _layout_blurb(self) -> str:
        return (
            "FOLDER FLEXIBILITY: Do not assume src/ or tests/ always exist. "
            "Discover the real repo layout. "
            "code_root=%r test_root=%r. "
            "If a root is missing, use an empty string and work with the actual tree. "
            "Never force creating both roots if the stack only needs one."
            % (self.cfg.code_root, self.cfg.test_root)
        )

    def _listed_artifacts(self, names: List[str]) -> str:
        lines = ["Work directory: %s" % self.work, "Read these files with tools before answering:"]
        for name in names:
            path = self.artifact(name)
            status = "present" if path.is_file() else "MISSING"
            lines.append("- %s (%s)" % (path, status))
        lines.append(
            "Use tools to inspect the repository. "
            "An empty or thin answer is valid only after you have inspected the tree."
        )
        return "\n".join(lines)

    def _engineering_rules(self) -> str:
        path = engine_root() / "docs" / "engineering.md"
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return ""

    def _prompt(self, role: str, body: List[str]) -> str:
        parts = [
            self.persona(role),
            "",
            self._engineering_rules(),
            "",
            self._layout_blurb(),
            "",
            *body,
        ]
        return "\n".join(parts)

    def _snapshot(self) -> dict:
        if not gitutil.is_git_repo(self.repo):
            return {"head": "", "paths": [], "entries": {}}
        return gitutil.snapshot(self.repo)

    def _run_start_entries(self, phase_before: dict) -> dict:
        """Content ids dirty when this run began. Missing start falls back to hop start."""
        start = (self.state.git or {}).get("start")
        if isinstance(start, dict) and ("entries" in start or "paths" in start):
            return dict(start.get("entries") or {})
        return dict(phase_before.get("entries") or {})

    def _fence_error(
        self,
        phase: str,
        roots: List[str],
        bad: Sequence[str],
        dirty: Sequence[str],
    ) -> str:
        dirty_set = set(dirty)
        outside = [p for p in bad if p not in dirty_set]
        parts = []
        if outside:
            parts.append(
                "%s wrote outside allowed roots %s: %s" % (phase, roots, ", ".join(outside))
            )
        if dirty:
            parts.append(
                "%s mutated already-dirty paths (dirty since run start): %s"
                % (phase, ", ".join(dirty))
            )
        return "; ".join(parts) or ("%s write fence violation" % phase)

    def _verify_write(self, phase: str, allowed: List[str], before: Any) -> None:
        if isinstance(before, dict):
            before_snap = before
        else:
            before_snap = {"head": "", "paths": list(before or []), "entries": {}}
        after = self._snapshot()
        delta = gitutil.changed_paths(self.repo, before_snap, after)
        work_root = ".team/work/%s" % self.state.slug
        roots = explicit_roots(allowed)
        ok, bad = gitutil.verify_delta(
            delta,
            roots,
            always_allowed=[work_root, ".team/work"],
        )
        b_entries = dict(before_snap.get("entries") or {})
        a_entries = dict(after.get("entries") or {})
        dirty = gitutil.already_dirty_mutations(
            delta,
            self._run_start_entries(before_snap),
            b_entries,
            a_entries,
            exempt_roots=(work_root, ".team/work"),
        )
        for path in dirty:
            if path not in bad:
                bad.append(path)
            if path in ok:
                ok.remove(path)
        gitutil.write_path_list(self.work / "git" / ("after-%s.txt" % phase), delta)
        report = gitutil.describe_verify(
            phase,
            delta,
            bad,
            roots,
            head_before=str(before_snap.get("head") or ""),
            head_after=str(after.get("head") or ""),
            already_dirty=dirty,
        )
        write_text(self.work / "git" / ("verify-%s.md" % phase), report)
        head_changed = bool(
            before_snap.get("head")
            and after.get("head")
            and before_snap.get("head") != after.get("head")
        )
        if not roots:
            self.log("git verify %s: no root set, advisory only (%d paths)" % (phase, len(delta)))
            return
        if bad:
            raise PipelineError(self._fence_error(phase, roots, bad, dirty))
        if head_changed:
            self.log(
                "git verify %s: HEAD changed %s -> %s"
                % (phase, before_snap.get("head"), after.get("head"))
            )
        elif not ok:
            self.log("git verify %s: no new paths (continuing)" % phase)
        else:
            self.log("git verify %s: %d path(s) ok" % (phase, len(ok)))

    def run(self, start: Optional[str] = None) -> State:
        if start:
            start_at = start
        else:
            start_at = _next_phase(self.state)
            if start_at is None:
                self.log("all phases already done")
                return self.state
        started = False
        order = _phase_order(self.state)
        for phase in order:
            if not started:
                if phase == start_at:
                    started = True
                else:
                    continue
            if self.done(phase) and start is None:
                continue
            skip_reason = self._skip_reason(phase)
            if skip_reason:
                self._skip(phase, skip_reason)
                continue
            handler = {
                "architect": self.phase_architect,
                "critic": self.phase_critic,
                "tdd-design": self.phase_tdd_design,
                "test-writer": self.phase_test_writer,
                "baseline": self.phase_baseline,
                "implementer": self.phase_implementer,
                "final-test": self.phase_final_test,
                "debugger": self.phase_debugger,
                "repair": self.phase_repair,
                "verify-test": self.phase_verify_test,
                "adversarial": self.phase_adversarial,
                "adversarial-test": self.phase_adversarial_test,
                "reviewer": self.phase_reviewer,
                "guardian": self.phase_guardian,
                "scout": self.phase_scout,
                "assess": self.phase_assess,
            }[phase]
            self.log("== %s (%s)" % (phase, self.cfg.assignment(role_for_phase(phase, self.state))))
            try:
                handler()
            except OptionalPhaseError as exc:
                self._skip(phase, str(exc))
                continue
            self.state.mark(phase)
            self.save()
            if self.cfg.dry_run and phase == "tdd-design":
                self.state.stop_reason = "dry_run"
                self.save()
                self.log("stop: dry-run (no test/production writes)")
                return self.state
            if self.cfg.dry_run and self.state.mode == "audit" and phase == "assess":
                self._write_audit_report()
                self.state.stop_reason = "dry_run"
                self.save()
                self.log("stop: dry-run (scout + assess only)")
                return self.state
            if self.cfg.stop_after and phase == self.cfg.stop_after:
                self.state.stop_reason = "stop_after:%s" % phase
                self.save()
                self.log("stop: --stop-after %s" % phase)
                return self.state
        self._write_followups()
        self.state.stop_reason = "complete"
        self.save()
        return self.state

    def _skip(self, phase: str, reason: str) -> None:
        if phase not in self.state.skipped:
            self.state.skipped.append(phase)
        self.state.mark(phase)
        self.save()
        self.log("%s  skipped (%s)" % (phase.upper(), reason))

    def _tests_passed(self) -> bool:
        return (self.state.final.get("status") or "") == "PASS"

    def _skip_reason(self, phase: str) -> str:
        user_skip = {
            "critic",
            "adversarial",
            "guardian",
            "debugger",
            "repair",
            "verify-test",
            "adversarial-test",
        }
        if self.should_skip(phase) and phase in user_skip:
            return "requested"
        if phase == "debugger" and self._tests_passed():
            return "tests passed"
        if phase == "repair":
            if self._tests_passed():
                return "tests passed"
            if "debugger" in self.state.skipped:
                return "no diagnosis"
            owner = self.state.diagnosis_owner
            if owner not in ("implementer", "test-writer"):
                return "owner=%s cannot auto-repair" % (owner or "unknown")
        if phase == "verify-test":
            if "repair" in self.state.skipped:
                return "no repair"
        if phase == "adversarial-test" and "adversarial" in self.state.skipped:
            return "no adversarial tests"
        return ""

    def phase_architect(self) -> None:
        prompt = self._prompt(
            "architect",
            [
                self._listed_artifacts(["brief.md"]),
                "Feature brief is in brief.md. Map the real code structure, then design.",
                "Return design_markdown plus code_root, test_root, acceptance_criteria,",
                "structural_touchpoints, and invariants. No function bodies.",
            ],
        )
        result = self.invoke("architect", "architect", prompt, "design.json")
        out = result.output
        design = as_str(out.get("design_markdown")) or "(empty design)"
        self.write_artifact("design.md", design)
        if not self.cfg.code_root:
            self.cfg.code_root = as_str(out.get("code_root"))
        if not self.cfg.test_root:
            self.cfg.test_root = as_str(out.get("test_root"))
        self.log("design written; code_root=%s test_root=%s" % (self.cfg.code_root, self.cfg.test_root))

    def phase_critic(self) -> None:
        if self.should_skip("critic"):
            return
        prompt = self._prompt(
            "critic",
            [
                self._listed_artifacts(["brief.md", "design.md"]),
                "Try to KILL the design. Do not help the architect.",
                "Run every attack in the persona. accepts=true only if none land",
                "and the brief is covered by testable criteria.",
                "If you reject, issues[] are the hits (max 10).",
            ],
        )
        result = self.invoke("critic", "critic", prompt, "critic.json")
        out = result.output
        self.write_artifact("critic.md", as_str(out.get("critic_markdown")) or json.dumps(out, indent=2))
        attacks = as_list(out.get("attacks"))
        landed = [
            as_str(a.get("hit") or a.get("question"))
            for a in attacks
            if isinstance(a, dict) and a.get("lands")
        ]
        if as_bool(out.get("accepts"), False):
            self.log("critic accepted (design survived)")
            return
        self.log("critic rejected; one architect revision")
        issues = as_list(out.get("issues")) or landed
        self._log_items(findings_mod.items_from_strings(issues, severity="issue"))
        prompt = self._prompt(
            "architect",
            [
                self._listed_artifacts(["brief.md", "design.md", "critic.md"]),
                "The critic tried to kill the design. Address the hits. Do not inflate scope.",
                "Issues: " + json.dumps(issues),
                "Revise design_markdown. Stay structure-level. No function bodies.",
            ],
        )
        result = self.invoke("architect", "architect-revise", prompt, "design.json", resume=True)
        out = result.output
        design = as_str(out.get("design_markdown")) or self.read_artifact("design.md")
        self.write_artifact("design.md", design)
        if not self.cfg.code_root:
            self.cfg.code_root = as_str(out.get("code_root"))
        if not self.cfg.test_root:
            self.cfg.test_root = as_str(out.get("test_root"))

    def phase_tdd_design(self) -> None:
        prompt = self._prompt(
            "tdd-design",
            [
                self._listed_artifacts(["brief.md", "design.md", "critic.md"]),
                "Produce the test contract. Do not write test files or production files.",
                "If criteria are unclear, set ready=false and list at most 10 questions.",
                "If ready, set ready=true, questions=[], and fill test_contract_markdown.",
            ],
        )
        result = self.invoke("tdd-design", "tdd-design", prompt, "tdd_design.json")
        out = result.output
        if not as_bool(out.get("ready"), False) and as_list(out.get("questions")):
            answers = self.consult("architect", as_list(out.get("questions")), "tdd-design")
            prompt = self._prompt(
                "tdd-design",
                [
                    self._listed_artifacts(["brief.md", "design.md"]),
                    "Architect answers:\n" + answers,
                    "Now produce the test contract. ready must be true.",
                ],
            )
            result = self.invoke(
                "tdd-design", "tdd-design-write", prompt, "tdd_design.json", resume=True
            )
            out = result.output
        contract = as_str(out.get("test_contract_markdown")) or "(empty test contract)"
        self.write_artifact("test-contract.md", contract)
        self.log("test contract written")

    def phase_test_writer(self) -> None:
        before = self._snapshot()
        prompt = self._prompt(
            "test-writer",
            [
                self._listed_artifacts(
                    ["brief.md", "design.md", "test-contract.md", "tdd-summary.md"]
                ),
                "CONSULT GATE ONLY. Do not write files yet.",
                "If clear enough, ready=true, consult=\"none\", questions=[].",
                "If blocked, ready=false, consult one of tdd-design|architect, max 10 questions.",
            ],
        )
        gate = self.invoke(
            "test-writer",
            "test-writer-gate",
            prompt,
            "gate.json",
            capability="read-only",
        )
        answers = "(no consult; test-writer ready)"
        gout = gate.output
        if not as_bool(gout.get("ready"), False) and as_list(gout.get("questions")):
            answers = self.consult(
                as_str(gout.get("consult")) or "tdd-design",
                as_list(gout.get("questions")),
                "test-writer",
            )
        prompt = self._prompt(
            "test-writer",
            [
                self._listed_artifacts(["brief.md", "design.md", "test-contract.md"]),
                "WRITE TESTS NOW. Edit ONLY under test_root=%r." % self.cfg.test_root,
                "NEVER edit production (code_root=%r)." % self.cfg.code_root,
                "Consult answers:\n" + answers,
                "Return summary and paths_touched (test paths only).",
            ],
        )
        result = self.invoke(
            "test-writer",
            "test-writer",
            prompt,
            "write_summary.json",
            capability="write-tests",
            resume=True,
        )
        summary = as_str(result.output.get("summary")) or "(no tdd summary)"
        self.write_artifact("tdd-summary.md", summary)
        self._verify_write("test-writer", [self.cfg.test_root], before)

    def phase_baseline(self) -> None:
        cmd = testhost.discover_test_command(self.repo, self.cfg.test_command)
        self.cfg.test_command = cmd
        run = testhost.run_suite(self.repo, cmd, timeout=self.cfg.phase_timeout)
        self.state.baseline = run
        self.write_artifact("baseline-report.md", testhost.render_report("Baseline test run", run))
        self.log("baseline %s (exit=%s)" % (run["status"], run["exit"]))

    def phase_implementer(self) -> None:
        before = self._snapshot()
        prompt = self._prompt(
            "implementer",
            [
                self._listed_artifacts(
                    [
                        "brief.md",
                        "design.md",
                        "test-contract.md",
                        "tdd-summary.md",
                        "baseline-report.md",
                    ]
                ),
                "CONSULT GATE ONLY. Do not write files yet.",
                "If clear, ready=true, consult=\"none\", questions=[].",
                "If blocked, ready=false, consult exactly one of tdd-design|test-writer|architect,",
                "max 10 questions.",
            ],
        )
        gate = self.invoke(
            "implementer",
            "implementer-gate",
            prompt,
            "gate.json",
            capability="read-only",
        )
        answers = "(no consult; implementer ready)"
        gout = gate.output
        if not as_bool(gout.get("ready"), False) and as_list(gout.get("questions")):
            answers = self.consult(
                as_str(gout.get("consult")) or "architect",
                as_list(gout.get("questions")),
                "implementer",
            )
        prompt = self._prompt(
            "implementer",
            [
                self._listed_artifacts(
                    ["brief.md", "design.md", "test-contract.md", "tdd-summary.md"]
                ),
                "WRITE PRODUCTION CODE NOW. Edit ONLY under code_root=%r." % self.cfg.code_root,
                "NEVER edit tests (test_root=%r). Never weaken/skip/delete tests." % self.cfg.test_root,
                "Consult answers:\n" + answers,
                "Stay inside the design invariants. Return summary and paths_touched.",
            ],
        )
        result = self.invoke(
            "implementer",
            "implementer",
            prompt,
            "write_summary.json",
            capability="write-code",
            resume=True,
        )
        summary = as_str(result.output.get("summary")) or "(no impl summary)"
        self.write_artifact("impl-summary.md", summary)
        self._verify_write("implementer", [self.cfg.code_root], before)

    def phase_final_test(self) -> None:
        cmd = testhost.discover_test_command(self.repo, self.cfg.test_command)
        self.cfg.test_command = cmd
        run = testhost.run_suite(self.repo, cmd, timeout=self.cfg.phase_timeout)
        comparison = testhost.compare(self.state.baseline, run)
        run = dict(run)
        run["comparison"] = comparison
        self.state.final = run
        md = testhost.render_report("Final test run", run, comparison)
        md = self._maybe_tester_agent(md, cmd, run, "tester")
        self.write_artifact("test-report.md", md)
        self.log("final %s verdict=%s" % (run["status"], comparison["verdict"]))

    def _maybe_tester_agent(self, host_md: str, cmd: str, run: Dict[str, Any], phase: str) -> str:
        if self.cfg.assignment("tester") not in ("claude", "grok", "fake"):
            return host_md
        prompt = self._prompt(
            "tester",
            [
                self._listed_artifacts(["baseline-report.md", "tdd-summary.md", "impl-summary.md"]),
                "The orchestrator already ran: %s (exit %s, status %s)."
                % (cmd, run.get("exit"), run.get("status")),
                "Do not edit files. You may re-run the same command.",
                "Your passed flag is advisory; the orchestrator exit code is authoritative.",
                "Fill report_markdown with commentary (failing names, surprises).",
            ],
        )
        try:
            result = self.invoke("tester", phase, prompt, "tester.json", capability="execute")
        except PipelineError as exc:
            self.log("tester agent failed (host report kept): %s" % exc)
            return host_md
        extra = as_str(result.output.get("report_markdown"))
        if not extra:
            return host_md
        return host_md.rstrip() + "\n\n## Tester agent\n\n" + extra.rstrip() + "\n"

    def phase_debugger(self, *, seq_applied: Optional[List[Dict[str, Any]]] = None) -> None:
        if self.state.final.get("status") == "PASS":
            return
        extra_prompt = ""
        if seq_applied:
            extra_prompt = (
                "SEQ APPLY. Set disposition to retry, skip, or reopen.\n"
                "retry = this class is wrong. skip = drop this class. "
                "reopen = an earlier applied class was wrong; set reopen_id to that id.\n"
                "Do not reopen yourself. Applied classes:\n"
                + json.dumps(seq_applied, indent=2)
            )
        prompt = self._prompt(
            "debugger",
            [
                self._listed_artifacts(
                    [
                        "design.md",
                        "test-contract.md",
                        "baseline-report.md",
                        "test-report.md",
                        "impl-summary.md",
                    ]
                ),
                "Tests failed. Diagnose root cause. Do not edit files.",
                "owner must be one of implementer|test-writer|architect|unknown.",
                extra_prompt,
            ],
        )
        result = self.invoke("debugger", "debugger", prompt, "debugger.json")
        md = as_str(result.output.get("diagnosis_markdown")) or json.dumps(result.output, indent=2)
        self.write_artifact("diagnosis.md", md)
        owner = as_str(result.output.get("owner")) or "unknown"
        self.state.diagnosis_owner = owner
        self.state.diagnosis_disposition = as_str(result.output.get("disposition")) or "retry"
        self.state.diagnosis_reopen_id = as_str(result.output.get("reopen_id"))
        self.log("debugger owner=%s" % owner)
        cause = as_str(result.output.get("root_cause")) or as_str(
            result.output.get("diagnosis_markdown")
        )
        self._log_items(
            [
                {
                    "severity": "high",
                    "title": "owner=%s" % owner,
                    "evidence": cause,
                    "path": "",
                    "kind": "implementation" if owner == "implementer" else "test",
                }
            ]
        )

    def phase_repair(self) -> None:
        owner = self.state.diagnosis_owner or "implementer"
        before = self._snapshot()
        if owner == "test-writer":
            prompt = self._prompt(
                "test-writer",
                [
                    self._listed_artifacts(
                        ["design.md", "test-contract.md", "diagnosis.md", "test-report.md"]
                    ),
                    "REPAIR. Diagnosis says the tests are wrong. Fix tests only.",
                    "Edit ONLY under test_root=%r. NEVER edit production." % self.cfg.test_root,
                    "Return summary and paths_touched.",
                ],
            )
            result = self.invoke(
                "test-writer",
                "repair-test-writer",
                prompt,
                "write_summary.json",
                capability="write-tests",
                resume=True,
            )
            self.write_artifact(
                "repair-summary.md",
                as_str(result.output.get("summary")) or "(no repair summary)",
            )
            self._verify_write("repair", [self.cfg.test_root], before)
        else:
            prompt = self._prompt(
                "implementer",
                [
                    self._listed_artifacts(
                        ["design.md", "test-contract.md", "diagnosis.md", "test-report.md"]
                    ),
                    "REPAIR. Diagnosis says production is wrong. Fix production only.",
                    "Edit ONLY under code_root=%r. NEVER edit tests." % self.cfg.code_root,
                    "Return summary and paths_touched.",
                ],
            )
            result = self.invoke(
                "implementer",
                "repair-implementer",
                prompt,
                "write_summary.json",
                capability="write-code",
                resume=True,
            )
            self.write_artifact(
                "repair-summary.md",
                as_str(result.output.get("summary")) or "(no repair summary)",
            )
            self._verify_write("repair", [self.cfg.code_root], before)
        self.log("repair via %s" % owner)

    def phase_verify_test(self) -> None:
        cmd = testhost.discover_test_command(self.repo, self.cfg.test_command)
        self.cfg.test_command = cmd
        run = testhost.run_suite(self.repo, cmd, timeout=self.cfg.phase_timeout)
        comparison = testhost.compare(self.state.baseline, run)
        run = dict(run)
        run["comparison"] = comparison
        self.state.final = run
        md = testhost.render_report("Verify test run (after repair)", run, comparison)
        md = self._maybe_tester_agent(md, cmd, run, "tester-verify")
        self.write_artifact("verify-test-report.md", md)
        self.write_artifact("test-report.md", md)
        self.log("verify %s verdict=%s" % (run["status"], comparison["verdict"]))

    def phase_adversarial(self) -> None:
        before = self._snapshot()
        prompt = self._prompt(
            "adversarial",
            [
                self._listed_artifacts(
                    [
                        "design.md",
                        "test-contract.md",
                        "tdd-summary.md",
                        "impl-summary.md",
                        "test-report.md",
                    ]
                ),
                "Hunt attack vectors, then WRITE tests under test_root=%r that try to break the implementation."
                % self.cfg.test_root,
                "Do not edit production (code_root=%r)." % self.cfg.code_root,
                "Do not weaken existing tests. At most 15 vectors, highest risk first.",
                "Return vectors, adversarial_markdown, and paths_touched (new test paths).",
            ],
        )
        result = self.invoke(
            "adversarial",
            "adversarial",
            prompt,
            "adversarial.json",
            capability="write-tests",
        )
        md = as_str(result.output.get("adversarial_markdown")) or json.dumps(
            result.output, indent=2
        )
        self.write_artifact("adversarial.md", md)
        self._verify_write("adversarial", [self.cfg.test_root], before)
        vectors = as_list(result.output.get("vectors"))
        self.log("adversarial %d vector(s)" % len(vectors))
        self._log_items(
            [
                {
                    "severity": "high",
                    "title": as_str(v.get("title")) or "(untitled)",
                    "evidence": as_str(v.get("threat")),
                    "path": as_str(v.get("path")),
                    "kind": "test",
                }
                for v in vectors
                if isinstance(v, dict)
            ]
        )

    def phase_adversarial_test(self) -> None:
        cmd = testhost.discover_test_command(self.repo, self.cfg.test_command)
        self.cfg.test_command = cmd
        run = testhost.run_suite(self.repo, cmd, timeout=self.cfg.phase_timeout)
        comparison = testhost.compare(self.state.final or self.state.baseline, run)
        run = dict(run)
        run["comparison"] = comparison
        self.state.adversarial_run = run
        md = testhost.render_report("Adversarial test run", run, comparison)
        self.write_artifact("adversarial-test-report.md", md)
        self.log("adversarial-test %s verdict=%s" % (run["status"], comparison["verdict"]))

    def phase_reviewer(self) -> None:
        if self.state.mode == "audit":
            self.phase_status_reviewer()
            return
        if self.state.mode == "range":
            self.phase_range_reviewer()
            return
        self._begin_review_attempt()
        assignment = self.cfg.assignment("reviewer")
        if assignment == "both":
            runtimes = ["claude", "grok"]
        else:
            runtimes = [assignment]
        artifacts = [
            "brief.md",
            "design.md",
            "critic.md",
            "test-contract.md",
            "tdd-summary.md",
            "impl-summary.md",
            "baseline-report.md",
            "test-report.md",
            "adversarial.md",
            "diagnosis.md",
        ]

        def one(runtime: str) -> Result:
            prompt = self._prompt(
                "reviewer",
                [
                    self._listed_artifacts(artifacts),
                    "READ-ONLY. Inspect the actual files and git status.",
                    "Summaries are claims, not evidence.",
                    self._reviewer_finding_rules(),
                    "You are the %s reviewer. Do not assume another reviewer exists."
                    % runtime,
                ],
            )
            return self.invoke(
                "reviewer",
                "reviewer-%s" % runtime,
                prompt,
                "review.json",
                runtime_name=runtime,
            )

        parts = []
        if len(runtimes) == 1:
            ordered = runtimes
            results = [one(runtimes[0])]
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futs = [(rt, pool.submit(one, rt)) for rt in runtimes]
                ordered = [rt for rt, _ in futs]
                results = [fut.result() for _, fut in futs]
        for rt, result in zip(ordered, results):
            md = as_str(result.output.get("review_markdown")) or as_str(result.output.get("summary"))
            self.write_artifact("review-%s.md" % rt, md)
            parts.append((rt, result.output, md))
        merged = merge_reviews(parts)
        self.write_artifact("review.md", merged)
        self.log("review written (%s)" % ", ".join(runtimes))
        self._log_items(findings_mod.collect_review_findings(self.work))

    def _verify_readonly(self, phase: str, before: Any) -> None:
        if not gitutil.is_git_repo(self.repo):
            self.log("git verify %s: skipped (not a git repo)" % phase)
            return
        self._verify_write(phase, [".team/work"], before)

    def _write_audit_report(self) -> None:
        status = self.read_artifact("status.md") or "(no status)"
        review = self.read_artifact("review.md") or "(no review yet)"
        combined = "# Status\n\n%s\n\n# Review\n\n%s\n" % (status.rstrip(), review.rstrip())
        self.write_artifact("report.md", combined)

    def phase_scout(self) -> None:
        before = self._snapshot()
        prompt = self._prompt(
            "scout",
            [
                self._listed_artifacts(["brief.md"]),
                "Query: %s" % json.dumps(self.state.brief),
                "Repo: %s" % json.dumps(str(self.repo)),
                "Thoroughness: %s (quick | medium | thorough)."
                % (self.state.depth or self.cfg.depth),
                "Inspect this path. Inventory layout, git branches, launchers, builds,",
                "empty/broken files, TODOs.",
                "Return components[] with name, path, state (done|wip|missing|external|broken), evidence.",
                "roots[] are top-level areas you inspected. notes is optional.",
                "An empty components list is valid only after you actually listed the tree.",
                "Do not edit files. Do not implement anything.",
            ],
        )
        result = self.invoke("scout", "scout", prompt, "scout.json")
        write_text(
            self.work / "scout.json",
            json.dumps(result.output, indent=2),
        )
        components = as_list(result.output.get("components"))
        lines = ["# Scout inventory", "", "components: %d" % len(components), ""]
        for item in components:
            lines.append(
                "- **%s** `%s` (%s) — %s"
                % (
                    item.get("name") or "?",
                    item.get("path") or "",
                    item.get("state") or "?",
                    item.get("evidence") or "",
                )
            )
        notes = as_str(result.output.get("notes"))
        if notes:
            lines.extend(["", "## Notes", notes])
        self.write_artifact("scout.md", "\n".join(lines) + "\n")
        self._verify_readonly("scout", before)
        self.log("scout %d component(s)" % len(components))

    def phase_assess(self) -> None:
        before = self._snapshot()
        scout_blob = self.read_artifact("scout.json") or self.read_artifact("scout.md")
        prompt = self._prompt(
            "architect",
            [
                self._listed_artifacts(["brief.md", "scout.md", "scout.json"]),
                "You are the architect in ASSESS mode. Read-only.",
                "No implementation plan unless the query asks for one.",
                "Query: %s" % json.dumps(self.state.brief),
                "Repo: %s" % json.dumps(str(self.repo)),
                "Scout inventory (verify with tools; do not trust blindly):\n" + scout_blob,
                "Inspect the tree yourself. Produce status_markdown covering finished, WIP,",
                "missing, broken, and risks, each with path-level evidence.",
                "summary is one or two sentences.",
            ],
        )
        result = self.invoke("architect", "assess", prompt, "status.json")
        status = as_str(result.output.get("status_markdown")) or "(empty status)"
        summary = as_str(result.output.get("summary")) or ""
        body = status if not summary else "%s\n\n%s" % (summary, status)
        self.write_artifact("status.md", body)
        self._verify_readonly("assess", before)
        self.log("status written")

    def phase_status_reviewer(self) -> None:
        self._begin_review_attempt()
        assignment = self.cfg.assignment("reviewer")
        if assignment == "both":
            runtimes = ["claude", "grok"]
        else:
            runtimes = [assignment]
        artifacts = ["brief.md", "scout.md", "scout.json", "status.md"]

        def one(runtime: str) -> Result:
            prompt = self._prompt(
                "reviewer",
                [
                    self._listed_artifacts(artifacts),
                    "You are the adversarial reviewer on a STATUS audit. READ-ONLY.",
                    "Your report is what the user consumes.",
                    "Query: %s" % json.dumps(self.state.brief),
                    "Repo: %s" % json.dumps(str(self.repo)),
                    "Inspect the actual tree. Confirm or refute done/WIP/missing/broken",
                    "claims with path-level evidence. Flag speculation.",
                    self._reviewer_finding_rules(),
                    "You are the %s reviewer. Do not assume another reviewer exists."
                    % runtime,
                ],
            )
            return self.invoke(
                "reviewer",
                "reviewer-%s" % runtime,
                prompt,
                "review.json",
                runtime_name=runtime,
            )

        before = self._snapshot()
        parts = []
        if len(runtimes) == 1:
            ordered = runtimes
            results = [one(runtimes[0])]
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futs = [(rt, pool.submit(one, rt)) for rt in runtimes]
                ordered = [rt for rt, _ in futs]
                results = [fut.result() for _, fut in futs]
        for rt, result in zip(ordered, results):
            md = as_str(result.output.get("review_markdown")) or as_str(result.output.get("summary"))
            self.write_artifact("review-%s.md" % rt, md)
            parts.append((rt, result.output, md))
        merged = merge_reviews(parts)
        self.write_artifact("review.md", merged)
        self._write_audit_report()
        self._verify_readonly("reviewer", before)
        self.log("audit review written (%s)" % ", ".join(runtimes))
        self._log_items(findings_mod.collect_review_findings(self.work))

    def phase_range_reviewer(self) -> None:
        self._begin_review_attempt()
        assignment = self.cfg.assignment("reviewer")
        if assignment == "both":
            runtimes = ["claude", "grok"]
        else:
            runtimes = [assignment]
        artifacts = ["brief.md", "range.md", "git/log.txt", "git/diff.patch"]

        def one(runtime: str) -> Result:
            prompt = self._prompt(
                "reviewer",
                [
                    self._listed_artifacts(artifacts),
                    "RANGE REVIEW. The orchestrator already collected the commit range.",
                    "git/log.txt and git/diff.patch are authoritative. Do not invent commits.",
                    "This is not a PR-only review: the range may be 'since the last reviewed-* tag'.",
                    "READ-ONLY. Inspect the actual files those commits touched.",
                    self._reviewer_finding_rules(),
                    "You are the %s reviewer. Do not assume another reviewer exists."
                    % runtime,
                ],
            )
            return self.invoke(
                "reviewer",
                "reviewer-%s" % runtime,
                prompt,
                "review.json",
                runtime_name=runtime,
            )

        before = self._snapshot()
        parts = []
        if len(runtimes) == 1:
            ordered = runtimes
            results = [one(runtimes[0])]
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futs = [(rt, pool.submit(one, rt)) for rt in runtimes]
                ordered = [rt for rt, _ in futs]
                results = [fut.result() for _, fut in futs]
        for rt, result in zip(ordered, results):
            md = as_str(result.output.get("review_markdown")) or as_str(result.output.get("summary"))
            self.write_artifact("review-%s.md" % rt, md)
            parts.append((rt, result.output, md))
        merged = merge_reviews(parts)
        self.write_artifact("review.md", merged)
        self._verify_readonly("reviewer", before)
        self.log("range review written (%s)" % ", ".join(runtimes))
        self._log_items(findings_mod.collect_review_findings(self.work))

    def phase_guardian(self) -> None:
        prompt = self._prompt(
            "guardian",
            [
                self._listed_artifacts(
                    [
                        "brief.md",
                        "design.md",
                        "critic.md",
                        "test-contract.md",
                        "tdd-summary.md",
                        "impl-summary.md",
                        "baseline-report.md",
                        "test-report.md",
                        "adversarial-test-report.md",
                        "apply-test-report.md",
                        "review.md",
                        "range.md",
                    ]
                ),
                "R = brief.md + target AGENTS.md. A = design.md. T = test-contract.md.",
                "I = the tree. V = test/apply reports.",
                "Evaluate R→A, A→T, T→I, and I→R. The last arrow is required.",
                "A green suite does not prove I→R. Do not edit files.",
            ],
        )
        result = self.invoke("guardian", "guardian", prompt, "guardian.json")
        md = as_str(result.output.get("guardian_markdown")) or json.dumps(result.output, indent=2)
        self.write_artifact("guardian.md", md)
        risks = as_list(result.output.get("risks"))
        self.log(
            "guardian %d risk(s)  %s"
            % (
                len(risks),
                findings_mod.format_chain(
                    result.output.get("chain"), color=style.color_enabled()
                ),
            )
        )
        self._log_items(findings_mod.collect_guardian_findings(self.work))

    def replan(self) -> None:
        review = self.read_artifact("review.md")
        if not review:
            raise PipelineError("replan needs review.md")
        self._begin_hop("architect", "apply: architect questions")
        prompt = self._prompt(
            "architect",
            [
                self._listed_artifacts(["brief.md", "design.md", "review.md", "guardian.md"]),
                "REPLAN: read the review. Produce questions_for_tdd and questions_for_implementer",
                "(each max 10, empty if none). Do not rewrite the design yet.",
            ],
        )
        rq = self.invoke("architect", "replan-questions", prompt, "replan_questions.json")
        blob = []
        q_tdd = as_list(rq.output.get("questions_for_tdd"))
        q_impl = as_list(rq.output.get("questions_for_implementer"))
        if q_tdd:
            blob.append(self.consult("tdd-design", q_tdd, "architect"))
        if q_impl:
            blob.append(self.consult("implementer", q_impl, "architect"))
        prompt = self._prompt(
            "architect",
            [
                self._listed_artifacts(["brief.md", "design.md", "review.md"]),
                "Consult answers:\n" + ("\n\n".join(blob) or "(no consults)"),
                "Write a DELTA design, not a full rewrite. Required headings:",
                "- Unchanged assumptions",
                "- Changed assumptions",
                "- New acceptance criteria",
                "- Removed acceptance criteria",
                "- Structural changes",
                "Still structure-level. No function bodies.",
            ],
        )
        self._begin_hop("architect", "apply: architect replan")
        result = self.invoke("architect", "replan", prompt, "design.json", resume=True)
        md = as_str(result.output.get("design_markdown")) or "(empty replan)"
        self.write_artifact("design-replan.md", md)
        if not self.cfg.code_root:
            self.cfg.code_root = as_str(result.output.get("code_root"))
        if not self.cfg.test_root:
            self.cfg.test_root = as_str(result.output.get("test_root"))
        self.state.mark("replan")
        self.state.stop_reason = "replan"
        self.save()
        self.log("replan written to design-replan.md")

    def apply_replan(self) -> None:
        delta = self.read_artifact("design-replan.md")
        if not delta:
            raise PipelineError("apply-replan needs design-replan.md (run team replan first)")
        self.write_artifact("design.md", delta)
        order = _phase_order(self.state)
        self.state.rewind_to("tdd-design", order)
        self.state.stop_reason = ""
        self.save()
        self.log("applied design-replan.md → design.md; resuming at tdd-design")
        self.run(start="tdd-design")

    def _reviewer_finding_rules(self) -> str:
        return (
            "Each finding MUST set kind to one of: architecture, implementation, test, note.\n"
            "- architecture: design, invariants, boundaries — architect will replan\n"
            "- implementation: production bug — implementer will patch\n"
            "- test: missing/wrong tests or contract — tdd-design + test-writer\n"
            "- note: open class or non-actionable; listed only\n"
            "At most 10 findings (severity, title, evidence, path, kind).\n"
            "Do not emit the JSON object until tools have read the listed artifacts "
            "and you have inspected the files in scope. A progress finding is not a review."
        )

    def _write_followups(self, *, seq: Optional[Dict[str, Any]] = None) -> None:
        items = findings_mod.collect_all(self.work)
        self.write_artifact(
            "followups.md",
            findings_mod.render_followups(items, seq=seq),
        )

    def apply_review(
        self,
        *,
        dry_run: bool = False,
        rereview: bool = True,
        seq: bool = False,
        skip_failed: bool = False,
        reopen: str = "",
    ) -> None:
        if self.state.mode == "audit":
            raise PipelineError("audit is read-only; apply needs a feature or range work slug")
        if not self.read_artifact("review.md"):
            raise PipelineError("apply needs review.md (run team review first)")

        reclassified = False
        try:
            findings = findings_mod.collect_review_findings(self.work)
        except findings_mod.FindingsError as exc:
            self.log("review results unusable (%s); refreshing recorded digests" % exc)
            self._refresh_recorded_review_digests()
            findings = findings_mod.collect_review_findings(self.work)
        if findings_mod.needs_classify(findings, work=self.work):
            self.log("review findings lack kind=; re-running reviewer")
            self.phase_reviewer()
            self.state.mark("reviewer")
            if self.state.mode != "audit" and "guardian" not in self.cfg.skip:
                try:
                    self.phase_guardian()
                    self.state.mark("guardian")
                except OptionalPhaseError as exc:
                    self._skip("guardian", str(exc))
            findings = findings_mod.collect_review_findings(self.work)
            reclassified = True
            self.save()

        findings = findings_mod.fill_missing_kinds(findings)
        guardian = findings_mod.collect_guardian_findings(self.work)
        items = findings + guardian
        seq_state = findings_mod.load_seq_state(self.work)
        findings_mod.write_findings(self.work, items, seq=seq_state)
        self._write_followups(seq=seq_state)
        groups = findings_mod.group_by_kind(items)
        self.write_artifact(
            "apply-plan.md",
            findings_mod.render_plan(groups, reclassified=reclassified),
        )
        if groups.get("unclassified"):
            self.state.stop_reason = "needs-classification"
            self.save()
            self.log("apply: unclassified findings remain; needs-classification")
            return
        if seq:
            if reopen:
                self._seq_reopen(reopen, review_findings=findings, guardian=guardian)
                return
            self._apply_seq(
                review_findings=findings,
                guardian=guardian,
                reclassified=reclassified,
                dry_run=dry_run,
                rereview=rereview,
                skip_failed=skip_failed,
            )
            return
        self.log(
            "apply plan: arch=%d impl=%d test=%d note=%d"
            % (
                len(groups["architecture"]),
                len(groups["implementation"]),
                len(groups["test"]),
                len(groups["note"]),
            )
        )
        self._log_items(items, sort=True, more_hint="apply-plan.md")

        actionable = findings_mod.actionable(items)
        if dry_run:
            self.state.stop_reason = "dry_run"
            self.save()
            self.log("stop: apply dry-run")
            return
        hops: List[str] = []
        if not actionable:
            self.write_artifact(
                "apply-summary.md",
                findings_mod.render_summary(
                    groups,
                    reclassified=reclassified,
                    suite_status="",
                    hops=[],
                    rereviewed=False,
                ),
            )
            self.state.stop_reason = "applied"
            self.save()
            self.log("apply: nothing actionable")
            return

        if groups["architecture"]:
            self.replan()
            delta = self.read_artifact("design-replan.md")
            if delta:
                self.write_artifact("design.md", delta)
                hops.append("architect replan → design.md")
                self.log("applied design-replan.md → design.md")

        if groups["architecture"] or groups["test"]:
            self._apply_tdd_design(items)
            hops.append("tdd-design contract")
            self._apply_test_writer(items)
            hops.append("test-writer")

        if groups["architecture"] or groups["implementation"]:
            self._apply_implementer(items)
            hops.append("implementer")

        cmd = testhost.discover_test_command(self.repo, self.cfg.test_command)
        self.cfg.test_command = cmd
        self._begin_hop("tester", "apply: host suite")
        run = testhost.run_suite(self.repo, cmd, timeout=self.cfg.phase_timeout)
        comparison = testhost.compare(self.state.final or self.state.baseline, run)
        run = dict(run)
        run["comparison"] = comparison
        self.state.final = run
        self.write_artifact(
            "apply-test-report.md",
            testhost.render_report("Apply test run", run, comparison),
        )
        hops.append("suite %s" % run.get("status"))
        self.log("apply-test %s" % run.get("status"))

        if run.get("status") != "PASS" and not self.should_skip("debugger"):
            try:
                self.phase_debugger()
                hops.append("debugger owner=%s" % (self.state.diagnosis_owner or "?"))
                if self.state.diagnosis_owner in ("implementer", "test-writer"):
                    self.phase_repair()
                    self.phase_verify_test()
                    hops.append("repair + verify")
            except OptionalPhaseError as exc:
                self._skip("debugger", str(exc))
                hops.append("debugger skipped (%s)" % exc)

        if rereview:
            self.phase_reviewer()
            if self.state.mode != "audit" and "guardian" not in self.cfg.skip:
                try:
                    self.phase_guardian()
                except OptionalPhaseError as exc:
                    self._skip("guardian", str(exc))
            self._write_followups()
            hops.append("closing review")

        self.write_artifact(
            "apply-summary.md",
            findings_mod.render_summary(
                groups,
                reclassified=reclassified,
                suite_status=str(run.get("status") or ""),
                hops=hops,
                rereviewed=rereview,
            ),
        )
        self.state.stop_reason = "applied"
        self.save()
        self.log("apply complete")
        self._log_items(
            findings_mod.collect_all(self.work), sort=True, more_hint="followups.md"
        )

    def _seq_reopen(
        self,
        fid: str,
        *,
        review_findings: List[Dict[str, Any]],
        guardian: List[Dict[str, Any]],
    ) -> None:
        seq = findings_mod.load_seq_state(self.work)
        try:
            seq = findings_mod.reopen_prefix(seq, fid)
        except findings_mod.FindingsError as exc:
            raise PipelineError(str(exc))
        findings_mod.write_findings(self.work, review_findings + guardian, seq=seq)
        self._write_followups(seq=seq)
        later = [
            row["id"]
            for row in findings_mod.latest_seq_rows(seq)
            if row.get("status") == "stale"
        ]
        item = findings_mod.seq_item_from_log(seq, fid)
        seq_dir = self.work / "seq" / fid
        seq_dir.mkdir(parents=True, exist_ok=True)
        write_text(
            seq_dir / "reopen.md",
            "# Reopen `%s`\n\n%s\n\nStale: %s\n"
            % (
                fid,
                item.get("title") or "",
                ", ".join("`%s`" % sid for sid in later) or "(none)",
            ),
        )
        self.write_artifact("apply-seq.md", findings_mod.render_seq_log(seq, slug=self.state.slug))
        self.state.stop_reason = "seq-reopened"
        self.save()
        self.log("seq: reopened %s; %d later class(es) stale" % (fid, len(later)))
        self.log("next: team apply %s --seq" % self.state.slug)

    def _apply_seq(
        self,
        *,
        review_findings: List[Dict[str, Any]],
        guardian: List[Dict[str, Any]],
        reclassified: bool,
        dry_run: bool,
        rereview: bool,
        skip_failed: bool,
    ) -> None:
        seq = findings_mod.load_seq_state(self.work)
        pool = list(review_findings) + list(guardian)
        if skip_failed and seq.get("failed"):
            failed_id = str(seq.get("failed") or "")
            dummy = {"id": failed_id, "title": "(skipped)", "kind": "", "path": ""}
            for item in pool:
                if findings_mod.finding_id(item) == failed_id:
                    dummy = dict(item)
                    dummy["id"] = failed_id
                    break
            seq = findings_mod.mark_seq_step(seq, dummy, status="skipped")
            findings_mod.write_findings(
                self.work, review_findings + guardian, seq=seq
            )
            self.log("seq: skipped failed class %s" % failed_id)

        ranked = []
        nxt = findings_mod.pick_next_seq(pool, seq)
        seen = set()
        probe = dict(seq)
        while nxt and nxt.get("id") not in seen:
            ranked.append(nxt)
            seen.add(nxt["id"])
            probe = findings_mod.mark_seq_step(probe, nxt, status="applied")
            nxt = findings_mod.pick_next_seq(pool, probe)
        self.write_artifact(
            "apply-plan.md",
            findings_mod.render_seq_plan(
                ranked, reclassified=reclassified, failed=seq.get("failed") or ""
            ),
        )
        self.log("seq plan: %d class(es) remaining" % len(ranked))
        self._log_items(ranked, more_hint="apply-plan.md")
        if dry_run:
            self.write_artifact("apply-seq.md", findings_mod.render_seq_log(seq, slug=self.state.slug))
            self.state.stop_reason = "dry_run"
            self.save()
            self.log("stop: apply --seq dry-run")
            return
        if not ranked:
            self.write_artifact("apply-seq.md", findings_mod.render_seq_log(seq, slug=self.state.slug))
            if findings_mod.seq_apply_complete(pool, seq):
                self.state.stop_reason = "applied"
                self.log("apply --seq: nothing remaining")
            else:
                self.log("apply --seq: nothing remaining but queue not exhausted")
            self.save()
            return

        while True:
            item = findings_mod.pick_next_seq(pool, seq)
            if item is None:
                break
            related = findings_mod.related_guardian(item, guardian)
            if as_str(item.get("source")) == "guardian":
                related = [
                    row
                    for row in related
                    if findings_mod.finding_id(row) != findings_mod.finding_id(item)
                ]
            ok = self._apply_one_seq(item, related, seq, rereview=rereview)
            seq = findings_mod.load_seq_state(self.work)
            if not ok:
                self._write_followups(seq=seq)
                self.write_artifact(
                    "apply-seq.md", findings_mod.render_seq_log(seq, slug=self.state.slug)
                )
                self.state.stop_reason = "seq-failed"
                self.save()
                self.log("apply --seq stopped: class %s failed" % item.get("id"))
                return
        seq = findings_mod.load_seq_state(self.work)
        self._write_followups(seq=seq)
        self.write_artifact("apply-seq.md", findings_mod.render_seq_log(seq, slug=self.state.slug))
        if findings_mod.seq_apply_complete(pool, seq):
            self.state.stop_reason = "applied"
            self.log("apply --seq complete")
        else:
            self.log("apply --seq: loop ended but queue not exhausted")
        self.save()

    def _apply_one_seq(
        self,
        item: Dict[str, Any],
        related: List[Dict[str, Any]],
        seq: Dict[str, Any],
        *,
        rereview: bool,
    ) -> bool:
        fid = item.get("id") or findings_mod.finding_id(item)
        item = dict(item)
        item["id"] = fid
        seq_dir = self.work / "seq" / fid
        (seq_dir / "prompts").mkdir(parents=True, exist_ok=True)
        bundle = [item] + list(related)
        dump = json.dumps(bundle, indent=2)
        write_text(seq_dir / "finding.json", dump)
        self.write_artifact(
            "apply-plan.md",
            findings_mod.render_seq_plan([item], failed=""),
        )
        shutil.copyfile(self.work / "apply-plan.md", seq_dir / "plan.md")
        start = self._snapshot()
        color = style.color_enabled()
        self.log(
            "seq class %s [%s] %s"
            % (
                fid,
                style.kind(as_str(item.get("kind")), enabled=color),
                style.link_tags(as_str(item.get("title")), enabled=color),
            )
        )
        hops: List[str] = []
        kind = item.get("kind")
        try:
            if kind == "architecture":
                self._apply_replan_seq(bundle)
                hops.append("architect replan → design.md")
                self._apply_tdd_design(bundle, thin=True)
                hops.append("tdd-design contract")
                self._apply_test_writer(bundle, thin=True)
                hops.append("test-writer")
                self._apply_implementer(bundle, thin=True)
                hops.append("implementer")
            elif kind == "test":
                self._apply_tdd_design(bundle, thin=True)
                hops.append("tdd-design contract")
                self._apply_test_writer(bundle, thin=True)
                hops.append("test-writer")
            else:
                self._apply_implementer(bundle, thin=True)
                hops.append("implementer")

            cmd = testhost.discover_test_command(self.repo, self.cfg.test_command)
            self.cfg.test_command = cmd
            self._begin_hop("tester", "apply: host suite")
            run = testhost.run_suite(self.repo, cmd, timeout=self.cfg.phase_timeout)
            comparison = testhost.compare(self.state.final or self.state.baseline, run)
            run = dict(run)
            run["comparison"] = comparison
            self.state.final = run
            self.write_artifact(
                "apply-test-report.md",
                testhost.render_report("Apply test run", run, comparison),
            )
            hops.append("suite %s" % run.get("status"))
            self.log("seq-test %s" % run.get("status"))

            if run.get("status") != "PASS" and not self.should_skip("debugger"):
                try:
                    applied_rows = [
                        row
                        for row in findings_mod.latest_seq_rows(seq)
                        if row.get("status") == "applied"
                    ]
                    self.phase_debugger(seq_applied=applied_rows)
                    hops.append("debugger owner=%s" % (self.state.diagnosis_owner or "?"))
                    self._log_seq_disposition(seq, item)
                    if self.state.diagnosis_owner in ("implementer", "test-writer"):
                        self.phase_repair()
                        self.phase_verify_test()
                        hops.append("repair + verify")
                        run = dict(self.state.final or run)
                except OptionalPhaseError as exc:
                    self._skip("debugger", str(exc))
                    hops.append("debugger skipped (%s)" % exc)

            suite = str((self.state.final or run).get("status") or run.get("status") or "")
            self._write_seq_checkpoint(seq_dir, item, start=start, hops=hops, suite=suite)
            self._seq_copy_artifacts(seq_dir)
            if suite != "PASS":
                seq = findings_mod.mark_seq_step(
                    seq, item, status="failed", hops=hops, suite=suite
                )
                findings_mod.write_findings(
                    self.work,
                    findings_mod.collect_all(self.work),
                    seq=seq,
                )
                write_text(
                    seq_dir / "summary.md",
                    findings_mod.render_seq_log(seq, slug=self.state.slug),
                )
                return False

            if rereview:
                self._begin_hop("reviewer", "seq: class review")
                self.phase_seq_review(seq_dir, bundle)
                hops.append("class review")

            seq = findings_mod.mark_seq_step(
                seq, item, status="applied", hops=hops, suite=suite
            )
            findings_mod.write_findings(
                self.work,
                findings_mod.collect_all(self.work),
                seq=seq,
            )
            self._write_seq_checkpoint(
                seq_dir, item, start=start, hops=hops, suite=suite, status="applied"
            )
            self._seq_copy_artifacts(seq_dir)
            write_text(
                seq_dir / "summary.md",
                "# Class `%s` applied\n\n- kind: %s\n- suite: %s\n"
                % (fid, kind, suite),
            )
            return True
        except PipelineError as exc:
            self._write_seq_checkpoint(
                seq_dir, item, start=start, hops=hops, suite="ERROR", status="failed"
            )
            seq = findings_mod.mark_seq_step(
                seq, item, status="failed", hops=hops, suite="ERROR"
            )
            findings_mod.write_findings(
                self.work,
                findings_mod.collect_all(self.work),
                seq=seq,
            )
            write_text(seq_dir / "summary.md", "failed: %s\n" % exc)
            self.log("seq class %s error: %s" % (fid, exc))
            return False

    def _write_seq_checkpoint(
        self,
        seq_dir: Path,
        item: Dict[str, Any],
        *,
        start: Dict[str, Any],
        hops: List[str],
        suite: str,
        status: str = "",
    ) -> None:
        end = self._snapshot()
        touched = gitutil.product_paths(gitutil.changed_paths(self.repo, start, end))
        patch = gitutil.worktree_diff(self.repo, touched) if gitutil.is_git_repo(self.repo) else ""
        if patch:
            write_text(seq_dir / "delta.patch", patch)
        assumptions = []
        if item.get("kind") == "architecture":
            assumptions = findings_mod.extract_assumptions(
                self.read_artifact("design-replan.md")
            )
        dump_json(
            seq_dir / "checkpoint.json",
            {
                "id": item.get("id") or "",
                "kind": item.get("kind") or "",
                "title": item.get("title") or "",
                "path": item.get("path") or "",
                "status": status or ("failed" if suite != "PASS" else "applied"),
                "head_before": start.get("head") or "",
                "head_after": end.get("head") or "",
                "start": start,
                "end": end,
                "touched": touched,
                "suite": suite,
                "hops": list(hops),
                "assumptions": assumptions,
            },
        )

    def _log_seq_disposition(self, seq: Dict[str, Any], item: Dict[str, Any]) -> None:
        disposition = (self.state.diagnosis_disposition or "retry").strip().lower()
        reopen_id = (self.state.diagnosis_reopen_id or "").strip()
        applied = {
            row["id"]
            for row in findings_mod.latest_seq_rows(seq)
            if row.get("status") == "applied"
        }
        if disposition == "reopen":
            if reopen_id and reopen_id in applied:
                self.log("debugger suggests reopen %s (not executed)" % reopen_id)
                self.log("  team apply %s --seq --reopen %s" % (self.state.slug, reopen_id))
            else:
                self.log(
                    "debugger disposition=reopen ignored (need applied reopen_id, got %r)"
                    % reopen_id
                )
                disposition = "retry"
        elif disposition == "skip":
            self.log("debugger suggests skip (not executed)")
            self.log("  team apply %s --seq --skip-failed" % self.state.slug)
        else:
            disposition = "retry"
        if disposition == "retry":
            path = as_str(item.get("path"))
            for row in findings_mod.latest_seq_rows(seq):
                if row.get("status") != "applied":
                    continue
                if row.get("kind") != "architecture":
                    continue
                if path and as_str(row.get("path")) == path:
                    self.log(
                        "same-path architecture %s is a reopen candidate"
                        % row.get("id")
                    )
        write_text(
            self.work / "seq" / str(item.get("id") or "") / "disposition.md",
            "disposition: %s\nreopen_id: %s\n"
            % (disposition, reopen_id if disposition == "reopen" else ""),
        )

    def _seq_copy_artifacts(self, seq_dir: Path) -> None:
        for name in (
            "apply-impl-summary.md",
            "apply-tdd-summary.md",
            "apply-test-report.md",
            "design-replan.md",
            "diagnosis.md",
            "repair-summary.md",
        ):
            src = self.work / name
            if src.is_file():
                shutil.copyfile(src, seq_dir / name)

    def _apply_replan_seq(self, items: List[Dict[str, Any]]) -> None:
        self._begin_hop("architect", "apply: architect questions")
        prompt = self._prompt(
            "architect",
            [
                self._listed_artifacts(["brief.md", "design.md", "apply-plan.md"]),
                "SEQ APPLY. Replan only this class. Do not read review.md.",
                "Findings:\n" + json.dumps(items, indent=2),
                "Produce questions_for_tdd and questions_for_implementer",
                "(each max 10, empty if none). Do not rewrite the design yet.",
            ],
        )
        rq = self.invoke("architect", "replan-questions", prompt, "replan_questions.json")
        blob = []
        q_tdd = as_list(rq.output.get("questions_for_tdd"))
        q_impl = as_list(rq.output.get("questions_for_implementer"))
        if q_tdd:
            blob.append(self.consult("tdd-design", q_tdd, "architect"))
        if q_impl:
            blob.append(self.consult("implementer", q_impl, "architect"))
        prompt = self._prompt(
            "architect",
            [
                self._listed_artifacts(["brief.md", "design.md", "apply-plan.md"]),
                "Consult answers:\n" + ("\n\n".join(blob) or "(no consults)"),
                "SEQ APPLY. Write a DELTA design for this class only. Required headings:",
                "- Unchanged assumptions",
                "- Changed assumptions",
                "- New acceptance criteria",
                "- Removed acceptance criteria",
                "- Structural changes",
                "Still structure-level. No function bodies.",
            ],
        )
        self._begin_hop("architect", "apply: architect replan")
        result = self.invoke("architect", "replan", prompt, "design.json", resume=True)
        md = as_str(result.output.get("design_markdown")) or "(empty replan)"
        self.write_artifact("design-replan.md", md)
        self.write_artifact("design.md", md)
        if not self.cfg.code_root:
            self.cfg.code_root = as_str(result.output.get("code_root"))
        if not self.cfg.test_root:
            self.cfg.test_root = as_str(result.output.get("test_root"))
        self.log("seq: applied design-replan.md → design.md")

    def phase_seq_review(self, seq_dir: Path, items: List[Dict[str, Any]]) -> None:
        assignment = self.cfg.assignment("reviewer")
        if assignment == "both":
            runtimes = ["claude", "grok"]
        else:
            runtimes = [assignment]
        artifacts = [
            "brief.md",
            "design.md",
            "test-contract.md",
            "apply-plan.md",
            "apply-impl-summary.md",
            "apply-tdd-summary.md",
            "apply-test-report.md",
        ]

        def one(runtime: str) -> Result:
            prompt = self._prompt(
                "reviewer",
                [
                    self._listed_artifacts(artifacts),
                    "CLASS REVIEW. Review only the class that apply --seq just closed.",
                    "The original review.md is out of scope. Do not rewrite it.",
                    "READ-ONLY. Inspect the actual files and git status.",
                    self._reviewer_finding_rules(),
                    "Class:\n" + json.dumps(items, indent=2),
                    "You are the %s reviewer. Do not assume another reviewer exists."
                    % runtime,
                ],
            )
            return self.invoke(
                "reviewer",
                "seq-reviewer-%s" % runtime,
                prompt,
                "review.json",
                runtime_name=runtime,
            )

        parts = []
        if len(runtimes) == 1:
            ordered = runtimes
            results = [one(runtimes[0])]
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futs = [(rt, pool.submit(one, rt)) for rt in runtimes]
                ordered = [rt for rt, _ in futs]
                results = [fut.result() for _, fut in futs]
        for rt, result in zip(ordered, results):
            md = as_str(result.output.get("review_markdown")) or as_str(
                result.output.get("summary")
            )
            write_text(seq_dir / ("review-%s.md" % rt), md)
            src = self.work / "prompts" / ("seq-reviewer-%s.result.json" % rt)
            if src.is_file():
                shutil.copyfile(src, seq_dir / "prompts" / src.name)
            parts.append((rt, result.output, md))
        merged = merge_reviews(parts)
        write_text(seq_dir / "review.md", merged)
        self.log("class review written (%s) → %s" % (", ".join(runtimes), seq_dir / "review.md"))
        if self.state.mode != "audit" and "guardian" not in self.cfg.skip:
            try:
                self._phase_seq_guardian(seq_dir, items)
            except OptionalPhaseError as exc:
                self.log("class guardian skipped (%s)" % exc)

    def _phase_seq_guardian(self, seq_dir: Path, items: List[Dict[str, Any]]) -> None:
        self._begin_hop("guardian", "seq: class guardian")
        prompt = self._prompt(
            "guardian",
            [
                self._listed_artifacts(
                    [
                        "brief.md",
                        "design.md",
                        "test-contract.md",
                        "apply-plan.md",
                        "apply-test-report.md",
                    ]
                ),
                "CLASS GUARDIAN. Evaluate only this applied class.",
                "Do not edit files. Do not write guardian.md at the slug root.",
                "Class:\n" + json.dumps(items, indent=2),
                "Evaluate R→A, A→T, T→I, and I→R.",
            ],
        )
        result = self.invoke("guardian", "seq-guardian", prompt, "guardian.json")
        md = as_str(result.output.get("guardian_markdown")) or json.dumps(
            result.output, indent=2
        )
        write_text(seq_dir / "guardian.md", md)
        src = self.work / "prompts" / "seq-guardian.result.json"
        if src.is_file():
            shutil.copyfile(src, seq_dir / "prompts" / src.name)

    def _apply_tdd_design(self, items: List[Dict[str, Any]], *, thin: bool = False) -> None:
        listed = ["brief.md", "design.md", "test-contract.md", "apply-plan.md"]
        if not thin:
            listed.insert(3, "review.md")
        prompt = self._prompt(
            "tdd-design",
            [
                self._listed_artifacts(listed),
                "APPLY REVIEW. Update the test contract so kind=test findings and any",
                "applied design delta are encoded. Do not write test or production files.",
                "Findings:\n" + json.dumps(items, indent=2),
                "If a contract exists, write a revised full contract, not a fragment.",
                "ready must be true unless you have at most 10 questions for the architect.",
            ],
        )
        self._begin_hop("tdd-design", "apply: tdd-design")
        result = self.invoke(
            "tdd-design", "tdd-design-apply", prompt, "tdd_design.json", resume=True
        )
        out = result.output
        if not as_bool(out.get("ready"), False) and as_list(out.get("questions")):
            answers = self.consult("architect", as_list(out.get("questions")), "tdd-design")
            prompt = self._prompt(
                "tdd-design",
                [
                    self._listed_artifacts(["brief.md", "design.md", "test-contract.md"]),
                    "Architect answers:\n" + answers,
                    "Now produce the updated test contract. ready must be true.",
                ],
            )
            result = self.invoke(
                "tdd-design", "tdd-design-apply-write", prompt, "tdd_design.json", resume=True
            )
            out = result.output
        contract = as_str(out.get("test_contract_markdown")) or self.read_artifact(
            "test-contract.md"
        )
        if contract:
            self.write_artifact("test-contract.md", contract)
        self.log("apply: test contract updated")

    def _apply_test_writer(self, items: List[Dict[str, Any]], *, thin: bool = False) -> None:
        before = self._snapshot()
        listed = ["brief.md", "design.md", "test-contract.md", "apply-plan.md"]
        if not thin:
            listed.insert(3, "review.md")
        prompt = self._prompt(
            "test-writer",
            [
                self._listed_artifacts(listed),
                "APPLY REVIEW. Encode kind=test findings (and the current contract).",
                "Edit ONLY under test_root=%r. NEVER edit production (code_root=%r)."
                % (self.cfg.test_root, self.cfg.code_root),
                "Findings:\n" + json.dumps(items, indent=2),
                "Return summary and paths_touched (test paths only).",
            ],
        )
        self._begin_hop("test-writer", "apply: test-writer")
        result = self.invoke(
            "test-writer",
            "test-writer-apply",
            prompt,
            "write_summary.json",
            capability="write-tests",
            resume=True,
        )
        self.write_artifact(
            "apply-tdd-summary.md",
            as_str(result.output.get("summary")) or "(no apply test summary)",
        )
        self._verify_write("apply-test-writer", [self.cfg.test_root], before)
        self.log("apply: tests")

    def _apply_implementer(self, items: List[Dict[str, Any]], *, thin: bool = False) -> None:
        before = self._snapshot()
        listed = ["brief.md", "design.md", "test-contract.md", "apply-plan.md"]
        if not thin:
            listed.insert(3, "review.md")
        prompt = self._prompt(
            "implementer",
            [
                self._listed_artifacts(listed),
                "APPLY REVIEW. Patch kind=implementation findings and realize any",
                "applied design delta. Edit ONLY under code_root=%r." % self.cfg.code_root,
                "NEVER edit tests (test_root=%r). Never weaken/skip/delete tests."
                % self.cfg.test_root,
                "Findings:\n" + json.dumps(items, indent=2),
                "Return summary and paths_touched.",
            ],
        )
        self._begin_hop("implementer", "apply: implementer")
        result = self.invoke(
            "implementer",
            "implementer-apply",
            prompt,
            "write_summary.json",
            capability="write-code",
            resume=True,
        )
        self.write_artifact(
            "apply-impl-summary.md",
            as_str(result.output.get("summary")) or "(no apply impl summary)",
        )
        self._verify_write("apply-implementer", [self.cfg.code_root], before)
        self.log("apply: implementation")


def role_for_phase(phase: str, state: Optional[State] = None) -> str:
    if phase == "repair" and state is not None:
        if state.diagnosis_owner == "test-writer":
            return "test-writer"
        return "implementer"
    return {
        "architect": "architect",
        "critic": "critic",
        "tdd-design": "tdd-design",
        "test-writer": "test-writer",
        "baseline": "tester",
        "implementer": "implementer",
        "final-test": "tester",
        "debugger": "debugger",
        "repair": "implementer",
        "verify-test": "tester",
        "adversarial": "adversarial",
        "adversarial-test": "tester",
        "reviewer": "reviewer",
        "guardian": "guardian",
        "scout": "scout",
        "assess": "architect",
    }.get(phase, phase)


def _phase_order(state: State) -> List[str]:
    if state.mode == "audit":
        return AUDIT_PHASE_ORDER
    if state.mode == "range":
        return RANGE_PHASE_ORDER
    return PHASE_ORDER


def _next_phase(state: State) -> Optional[str]:
    done = set(state.phases_done)
    for phase in _phase_order(state):
        if phase not in done:
            return phase
    return None


def start_feature(cfg: Config, brief: str, slug: str) -> Pipeline:
    repo = cfg.repo
    if not gitutil.is_git_repo(repo):
        raise PipelineError("%s is not a git repository" % repo)
    work = work_dir(repo, slug)
    if (work / "state.json").is_file() and not cfg.force:
        raise PipelineError("work already exists at %s (use --force or team resume %s)" % (work, slug))
    if work.exists() and cfg.force:
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / "consult").mkdir(exist_ok=True)
    (work / "git").mkdir(exist_ok=True)
    (work / "prompts").mkdir(exist_ok=True)
    write_text(work / "brief.md", brief.strip() + "\n")
    snap = gitutil.snapshot(repo)
    gitutil.write_path_list(work / "git" / "start.txt", snap["paths"])
    state = State(
        slug=slug,
        brief=brief.strip(),
        repo=str(repo),
        engine_root=str(engine_root()),
        code_root=cfg.code_root,
        test_root=cfg.test_root,
        test_command=cfg.test_command,
        assignment=dict(cfg.roles),
        git={"start": snap},
        mode="feature",
    )
    state.save(work)
    return Pipeline(cfg, state, work)


def start_audit(cfg: Config, query: str, slug: str) -> Pipeline:
    repo = cfg.repo
    work = work_dir(repo, slug)
    if (work / "state.json").is_file() and not cfg.force:
        raise PipelineError("work already exists at %s (use --force or team resume %s)" % (work, slug))
    if work.exists() and cfg.force:
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / "consult").mkdir(exist_ok=True)
    (work / "git").mkdir(exist_ok=True)
    (work / "prompts").mkdir(exist_ok=True)
    write_text(work / "brief.md", query.strip() + "\n")
    snap = gitutil.snapshot(repo) if gitutil.is_git_repo(repo) else {"head": "", "paths": []}
    gitutil.write_path_list(work / "git" / "start.txt", snap["paths"])
    state = State(
        slug=slug,
        brief=query.strip(),
        repo=str(repo),
        engine_root=str(engine_root()),
        assignment=dict(cfg.roles),
        git={"start": snap},
        mode="audit",
        depth=cfg.depth,
        phase="scout",
    )
    state.save(work)
    return Pipeline(cfg, state, work)


def start_range_review(
    cfg: Config,
    *,
    slug: str,
    pr: str = "",
    since: str = "",
) -> Pipeline:
    repo = cfg.repo
    if not gitutil.is_git_repo(repo):
        raise PipelineError("%s is not a git repository (range review needs git)" % repo)
    work = work_dir(repo, slug)
    if (work / "state.json").is_file() and not cfg.force:
        raise PipelineError("work already exists at %s (use --force or team resume %s)" % (work, slug))
    if work.exists() and cfg.force:
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / "consult").mkdir(exist_ok=True)
    (work / "git").mkdir(exist_ok=True)
    (work / "prompts").mkdir(exist_ok=True)
    if pr:
        log, diff, how = gitutil.pr_bundle(repo, pr)
        base, kind = pr, "pr"
        count = len([ln for ln in log.splitlines() if ln.strip()])
        desc = gitutil.describe_range(pr, "pr", count) + " [%s]" % how
    else:
        base, kind = gitutil.resolve_review_base(repo, since)
        log = gitutil.range_log(repo, base)
        diff = gitutil.range_diff(repo, base)
        count = gitutil.commit_count(repo, base)
        desc = gitutil.describe_range(base, kind, count)
        how = kind
    write_text(work / "brief.md", desc + "\n")
    write_text(work / "range.md", "# Range\n\n%s\n\n- base: `%s`\n- kind: %s\n- commits: %d\n" % (desc, base or "(root)", kind, count))
    write_text(work / "git" / "log.txt", log or "(empty range)\n")
    write_text(work / "git" / "diff.patch", diff or "(empty diff)\n")
    names = gitutil.paths_from_diff(diff)
    gitutil.write_path_list(work / "git" / "names.txt", names)
    snap = gitutil.snapshot(repo)
    gitutil.write_path_list(work / "git" / "start.txt", snap["paths"])
    state = State(
        slug=slug,
        brief=desc,
        repo=str(repo),
        engine_root=str(engine_root()),
        assignment=dict(cfg.roles),
        git={"start": snap, "range_base": base, "range_kind": kind, "range_how": how},
        mode="range",
        phase="reviewer",
        range_base=base,
        range_kind=kind,
        range_pr=pr,
        range_source=how,
    )
    state.save(work)
    return Pipeline(cfg, state, work)


def load_pipeline(cfg: Config, slug: str) -> Pipeline:
    work = work_dir(cfg.repo, slug)
    if not (work / "state.json").is_file():
        raise PipelineError("no run at %s" % work)
    state = State.load(work)
    if not cfg.code_root:
        cfg.code_root = state.code_root
    if not cfg.test_root:
        cfg.test_root = state.test_root
    if not cfg.test_command:
        cfg.test_command = state.test_command
    if state.depth:
        cfg.depth = state.depth
    for role, runtime in (state.assignment or {}).items():
        if role not in cfg.roles:
            continue
        if role in cfg.role_overrides:
            continue
        allowed = ROLES.get(role, {}).get("runtimes") or ()
        if runtime in allowed or runtime == "fake":
            cfg.roles[role] = runtime
    return Pipeline(cfg, state, work)
