from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from team import gitutil, testhost
from team.config import (
    AUDIT_PHASE_ORDER,
    PHASE_ORDER,
    ROLES,
    Config,
    persona_path,
    schema_path,
)
from team.merge import merge_reviews
from team.runners import Result, Runtime, runtime_for
from team.state import State, work_dir
from team.util import as_bool, as_list, as_str, engine_root, load_json, write_text


class PipelineError(RuntimeError):
    pass


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
        rt: Runtime = runtime_for(runtime_name)
        session_key = "%s:%s" % (role, runtime_name)
        sid = self.state.sessions.get(session_key, "")
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
            resume=resume and bool(sid),
            work=self.work,
            repo=self.repo,
            timeout=self.cfg.phase_timeout,
            extra=extra,
        )
        if result.session_id:
            self.state.sessions[session_key] = result.session_id
        write_text(
            self.work / "prompts" / ("%s.result.json" % phase),
            json.dumps(result.output, indent=2)[:200000],
        )
        if not result.success:
            raise PipelineError("%s/%s failed: %s" % (role, phase, result.error or "unknown"))
        return result

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

    def _verify_write(self, phase: str, allowed: List[str], before: List[str]) -> None:
        after = gitutil.porcelain_paths(self.repo)
        delta = gitutil.delta_paths(before, after)
        work_root = ".team/work/%s" % self.state.slug
        ok, bad = gitutil.verify_delta(
            delta,
            allowed,
            always_allowed=[work_root, ".team/work"],
        )
        gitutil.write_path_list(self.work / "git" / ("after-%s.txt" % phase), delta)
        report = gitutil.describe_verify(phase, delta, bad, allowed)
        write_text(self.work / "git" / ("verify-%s.md" % phase), report)
        if not allowed:
            self.log("git verify %s: no root set, advisory only (%d paths)" % (phase, len(delta)))
            return
        if bad:
            raise PipelineError(
                "%s wrote outside allowed roots %s: %s" % (phase, allowed, ", ".join(bad))
            )
        if not ok:
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
        order = AUDIT_PHASE_ORDER if self.state.mode == "audit" else PHASE_ORDER
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
            handler()
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
                "Does design.md satisfy brief.md?",
                "Set accepts=true only if the brief is covered by testable criteria.",
            ],
        )
        result = self.invoke("critic", "critic", prompt, "critic.json")
        out = result.output
        self.write_artifact("critic.md", as_str(out.get("critic_markdown")) or json.dumps(out, indent=2))
        if as_bool(out.get("accepts"), False):
            self.log("critic accepted")
            return
        self.log("critic rejected; one architect revision")
        issues = as_list(out.get("issues"))
        prompt = self._prompt(
            "architect",
            [
                self._listed_artifacts(["brief.md", "design.md", "critic.md"]),
                "The requirements critic rejected the design.",
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
        before = gitutil.porcelain_paths(self.repo)
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
        before = gitutil.porcelain_paths(self.repo)
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

    def phase_debugger(self) -> None:
        if self.state.final.get("status") == "PASS":
            return
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
            ],
        )
        result = self.invoke("debugger", "debugger", prompt, "debugger.json")
        md = as_str(result.output.get("diagnosis_markdown")) or json.dumps(result.output, indent=2)
        self.write_artifact("diagnosis.md", md)
        owner = as_str(result.output.get("owner")) or "unknown"
        self.state.diagnosis_owner = owner
        self.log("debugger owner=%s" % owner)

    def phase_repair(self) -> None:
        owner = self.state.diagnosis_owner or "implementer"
        before = gitutil.porcelain_paths(self.repo)
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
        before = gitutil.porcelain_paths(self.repo)
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
        self.log("adversarial %d vector(s)" % len(as_list(result.output.get("vectors"))))

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
                    "At most 10 findings (severity, title, evidence, path).",
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

    def _verify_readonly(self, phase: str, before: List[str]) -> None:
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
        before = gitutil.porcelain_paths(self.repo) if gitutil.is_git_repo(self.repo) else []
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
        before = gitutil.porcelain_paths(self.repo) if gitutil.is_git_repo(self.repo) else []
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
                    "At most 10 findings (severity, title, evidence, path).",
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

        before = gitutil.porcelain_paths(self.repo) if gitutil.is_git_repo(self.repo) else []
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

    def phase_guardian(self) -> None:
        prompt = self._prompt(
            "guardian",
            [
                self._listed_artifacts(
                    [
                        "brief.md",
                        "design.md",
                        "test-contract.md",
                        "test-report.md",
                        "review.md",
                    ]
                ),
                "What invariant could be violated despite tests passing?",
                "Do not edit files.",
            ],
        )
        result = self.invoke("guardian", "guardian", prompt, "guardian.json")
        md = as_str(result.output.get("guardian_markdown")) or json.dumps(result.output, indent=2)
        self.write_artifact("guardian.md", md)
        self.log("guardian %d risk(s)" % len(as_list(result.output.get("risks"))))

    def replan(self) -> None:
        review = self.read_artifact("review.md")
        if not review:
            raise PipelineError("replan needs review.md")
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
        result = self.invoke("architect", "replan", prompt, "design.json", resume=True)
        md = as_str(result.output.get("design_markdown")) or "(empty replan)"
        self.write_artifact("design-replan.md", md)
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

    def _write_followups(self) -> None:
        lines = ["# Open classes", ""]
        found = False
        for path in sorted((self.work / "prompts").glob("reviewer-*.result.json")):
            try:
                data = load_json(path)
            except Exception:
                continue
            for item in as_list(data.get("findings")):
                found = True
                title = item.get("title") or "(untitled)"
                sev = item.get("severity") or "?"
                loc = item.get("path") or ""
                lines.append("- **%s** %s%s" % (sev, title, (" (`%s`)" % loc) if loc else ""))
        gpath = self.work / "prompts" / "guardian.result.json"
        if gpath.is_file():
            try:
                data = load_json(gpath)
            except Exception:
                data = {}
            for item in as_list(data.get("risks")):
                found = True
                title = item.get("title") or "(untitled)"
                loc = item.get("path") or ""
                lines.append("- **invariant** %s%s" % (title, (" (`%s`)" % loc) if loc else ""))
        if not found:
            lines.append("- (none recorded in reviewer/guardian structured output)")
        self.write_artifact("followups.md", "\n".join(lines) + "\n")


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
