from __future__ import annotations

import hashlib
import json
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from team import findings as findings_mod
from team import gitutil, style, testhost
from team.config import (
    AUDIT_PHASE_ORDER,
    OPTIONAL_PHASES,
    PHASE_ORDER,
    RANGE_PHASE_ORDER,
    ROLES,
    Config,
    expand_reviewer,
    is_model_runtime,
    may_write,
    persona_path,
    schema_path,
)
from team.merge import merge_reviews
from team.runners import (
    Result,
    Runtime,
    describe_runtime_failure,
    is_quota_failure,
    inspect_progress_note,
    runtime_for,
    unfinished_inspect,
    unfinished_write,
)
from team.schemas import validate as validate_schema
from team.state import State, work_dir
from team import usage as usage_mod
from team.util import (
    as_bool,
    as_list,
    as_str,
    denied_write_code_roots,
    denied_write_test_roots,
    dump_json,
    engine_root,
    explicit_roots,
    load_json,
    posix,
    normalize_root,
    write_text,
)


class PipelineError(RuntimeError):
    pass


# Shared tree inventory. First inspect hop that emits census_markdown writes it;
# later hops read it so they do not recensus. Census is a map, not evidence.
# Diffs and working-tree files remain required reading.
CENSUS_ARTIFACT = "census.md"

# An artifact this small travels in the prompt instead of costing the hop a
# tool round trip. Deliberately conservative: see Pipeline._inline_choice.
INLINE_ARTIFACT_MAX = 4 * 1024
INLINE_TOTAL_MAX = 12 * 1024

# Artifacts that aggregate findings across roles. Never inlined at any size:
# a role's scope is what the orchestrator *hands* it, and pasting the whole
# plan into a test-writer's prompt hands it every implementation finding
# too. They stay listed, exactly as before, so a hop that genuinely needs the
# wider picture can still open one. The scoped findings for a hop arrive by
# their own route (_findings_prompt_lines), already filtered by kind.
# Names appended to a reused census. A cap, not a summary: past this many the
# map is stale enough that the count is the useful signal.
_CENSUS_MOVED_MAX = 40

# Accumulated context past which a warm chain costs more than it saves. A turn
# re-sends everything the session holds, so a long chain inverts: measured
# hops average ~54k context per turn, and a session carrying twice that is
# paying more per turn than a cold hop pays in total.
WARM_CONTEXT_CEILING = 120_000

CROSS_ROLE_ARTIFACTS = frozenset(
    {"apply-plan.md", "review.md", "guardian.md", "followups.md", "apply-seq.md"}
)

# HEAD copies of product law, for comparison. Live AGENTS.md is still R.
RANGE_HEAD_LAW = (
    ("AGENTS.md", "git/committed-AGENTS.md"),
    ("docs/protocol.md", "git/committed-docs-protocol.md"),
)


class OptionalPhaseError(PipelineError):
    """Optional role (guardian, critic, …) could not run. Skip, do not abort."""


class QuotaExhausted(PipelineError):
    """Provider refused on quota. The run is suspended, not failed.

    Everything already paid for stays on disk and the phase is unchanged, so
    ``team resume <slug>`` picks up at the same hop once the window resets.
    Required roles only — an optional role that hits the same wall still takes
    the ordinary skip, so a quota blip cannot discard a review already bought.
    """


def merge_design_delta(prior: str, delta: str) -> str:
    """Living design = prior kept (minus Removed) plus the delta copy."""
    prior = prior or ""
    delta = delta or ""
    if not prior.strip():
        return delta
    if not delta.strip():
        return prior
    removed = set()
    in_removed = False
    for raw in delta.splitlines():
        if raw.startswith("#"):
            in_removed = raw.lstrip("#").strip().lower() == "removed acceptance criteria"
            continue
        if not in_removed:
            continue
        text = raw.strip()
        if text.startswith("-"):
            text = text[1:].strip()
        if not text or text.lower() in ("none", "- none"):
            continue
        removed.add(text)
    kept = []
    for raw in prior.splitlines():
        text = raw.strip()
        item = text[1:].strip() if text.startswith("-") else ""
        if item and item in removed:
            continue
        kept.append(raw)
    body = "\n".join(kept).rstrip()
    extra = delta.strip()
    if body and extra:
        return body + "\n\n" + extra + "\n"
    return (body or extra) + ("\n" if (body or extra) else "")


class Pipeline:
    def __init__(self, cfg: Config, state: State, work: Path) -> None:
        self.cfg = cfg
        self.state = state
        self.work = work
        self.repo = Path(state.repo)
        self.log_lines: List[str] = []
        # reviewer="both" runs two invokes on this one Pipeline. Every mutation
        # of self.state, and every save() that serialises it, is under this lock:
        # asdict() walking state.sessions while the other thread assigns into it
        # raises "dictionary changed size during iteration", and two unsynchronised
        # read-modify-writes of last_review drop one reviewer's recording.
        self._state_lock = threading.RLock()
        # Bytes the last _listed_artifacts offered this hop, for the ledger.
        # Thread-local for the same reason the lock exists: reviewer="both"
        # builds two prompts on this one Pipeline at once, and a shared
        # attribute would bill one reviewer's listing to the other.
        self._tls = threading.local()
        # Live warm chains, (role, runtime, capability) -> session + context.
        # In-process only: a resumed run has no live session and starts cold.
        self._warm_chains: Dict[tuple, Dict[str, Any]] = {}
        # Paths this process wrote itself, path -> content id. The fence bills
        # a hop for what the hop wrote, not for these.
        self._orchestrator_writes: Dict[str, str] = {}
        self._seed_census()

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
        self.cfg.code_root = normalize_root(self.cfg.code_root)
        self.cfg.test_root = normalize_root(self.cfg.test_root)
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

    def _apply_repairs(self, *, repair: bool) -> bool:
        """Debug + repair on the apply rail is opt-in (team apply --repair).

        The diagnose/repair loop is the most expensive stretch of a hop and is
        headed for its own rail. Off by default, apply stops at needs-repair
        and leaves the failing suite for the caller.
        """
        return bool(repair) and not self.should_skip("debugger")

    def _repair_off_hint(self) -> str:
        if self.should_skip("debugger"):
            return "debug/repair skipped (requested); suite left failing"
        return "debug/repair off (opt in with team apply --repair); suite left failing"

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
        extra: Optional[Dict[str, Any]] = None,
        runtime_name: Optional[str] = None,
        write_verify: Optional[Callable[[Any], None]] = None,
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
        # Files are the handoff. A stored session is a log id, not a thread.
        # Cold by default: mint a new --session-id, never --resume / -r.
        # Under [run] warm the chain below may reuse one, and only then.
        sid, resume = self._warm_session(role, runtime_name, cap)
        extra = dict(extra or {})
        extra.setdefault("code_root", normalize_root(self.cfg.code_root))
        extra.setdefault("test_root", normalize_root(self.cfg.test_root))
        extra.setdefault("submodule_paths", self._submodule_paths())
        level = self.cfg.effort_for(role)
        if level:
            extra.setdefault("effort", level)
        before = self._snapshot_for_restore()
        complete_err: Optional[BaseException] = None
        result: Optional[Result] = None
        # Fence wraps complete: schema/runtime failure still verifies and restores.
        try:
            result = rt.complete(
                role=role,
                phase=phase,
                prompt=prompt,
                schema=self.schema(schema_name),
                capability=cap,
                session_id=sid,
                resume=resume,
                work=self.work,
                repo=self.repo,
                timeout=self.cfg.phase_timeout,
                extra=extra,
            )
        except BaseException as exc:
            complete_err = exc
        if result is not None:
            # Reads of state here have to be inside the lock too: _meta.attempt
            # is read from last_review while the sibling reviewer thread may be
            # rewriting it.
            with self._state_lock:
                if result.session_id:
                    self.state.sessions[session_key] = result.session_id
                attempt = (self.state.last_review or {}).get("attempt") or 0
                slug = self.state.slug
                range_base = self.state.range_base
            output = (
                dict(result.output)
                if isinstance(result.output, dict)
                else {"value": result.output}
            )
            output["_meta"] = {
                "slug": slug,
                "attempt": attempt,
                "phase": phase,
                "role": role,
                "runtime": runtime_name,
                "head": gitutil.head(self.repo) if gitutil.is_git_repo(self.repo) else "",
                "range_base": range_base,
                "num_turns": result.num_turns,
            }
            if result.usage is not None:
                output["_meta"]["usage"] = result.usage.to_dict()
            if result.num_turns is not None:
                output["num_turns"] = result.num_turns
            result_path = write_text(
                self.work / "prompts" / ("%s.result.json" % phase),
                json.dumps(output, indent=2),
            )
            if str(phase).startswith("reviewer-"):
                self._record_review_result(result_path, num_turns=result.num_turns)
            self._note_warm_hop(role, runtime_name, cap, result)
            self._record_usage(
                role,
                phase,
                runtime_name,
                result,
                prompt_bytes=len(prompt.encode("utf-8")),
                listed_bytes=getattr(self._tls, "listed_bytes", None),
            )
        if before is not None:
            self._fence_after_invoke(cap, phase, before, write_verify)
        if complete_err is not None:
            raise complete_err
        if result is None:
            raise PipelineError("%s/%s failed: runtime returned no result" % (role, phase))
        if not result.success:
            err = describe_runtime_failure(result)
            # Optional roles keep their existing skip, quota or not: the run
            # already paid for the reviewer, and dropping that artifact because
            # guardian ran out of budget costs more than the missing guardian.
            if ROLES.get(role, {}).get("optional"):
                raise OptionalPhaseError("%s: %s" % (phase, err))
            if is_quota_failure(result):
                with self._state_lock:
                    self.state.stop_reason = "quota"
                    self.save()
                raise QuotaExhausted(
                    "%s/%s suspended: %s\n"
                    "Nothing is lost. Resume when the window resets:\n"
                    "  team resume %s" % (role, phase, err, self.state.slug)
                )
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
        if unfinished_inspect(role=role, num_turns=result.num_turns, output=result.output):
            turns = result.num_turns
            premature = turns is None or turns <= 1
            progress = inspect_progress_note(result.output)
            # A 32-turn "drafting" already inspected. Retry is a new session
            # and doubles the bill. Only the 1-turn schema dump is retried.
            if extra.get("_inspect_retry") or (progress and not premature):
                if progress and not premature:
                    msg = (
                        "%s/%s failed: progress note after %s model turn(s) is not a review"
                        % (role, phase, turns)
                    )
                else:
                    msg = (
                        "%s/%s failed: finished in %s model turn(s) without inspecting the tree"
                        % (role, phase, turns)
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
                extra=extra,
                runtime_name=runtime_name,
                write_verify=write_verify,
            )
        after = self._snapshot() if before is not None else None
        delta = (
            gitutil.product_paths(gitutil.changed_paths(self.repo, before, after))
            if before is not None and after is not None
            else []
        )
        if unfinished_write(
            capability=cap,
            num_turns=result.num_turns,
            product_delta=delta,
            output=result.output,
        ):
            turns = result.num_turns
            if extra.get("_write_retry"):
                raise PipelineError(
                    "%s/%s failed: no product delta after %s model turn(s)"
                    % (role, phase, turns)
                )
            extra["_write_retry"] = True
            self.log(
                "%s wrote no product files in %s turn(s); retrying"
                % (phase, turns)
            )
            return self.invoke(
                role,
                phase,
                prompt
                + "\n\nPREVIOUS OUTPUT REJECTED: a write hop with no product-tree "
                "delta is not finished. Edit the files this role owns, then emit "
                "the summary.",
                schema_name,
                capability=capability,
                extra=extra,
                runtime_name=runtime_name,
                write_verify=write_verify,
            )
        self._adopt_census(result.output)
        return result

    def _warm_session(self, role: str, runtime: str, capability: str) -> tuple:
        """``(session_id, resume)`` for this hop. Cold unless a chain is live.

        Files stay the protocol: every prompt is self-contained and every
        artifact is written either way, so any link can be dropped and run
        cold without changing the result. That is what the warm/cold
        equivalence test pins, and it is why this is an accelerator rather
        than a second channel.

        A chain needs the same role, runtime **and capability**. Capability
        because a resumed hop re-declaring different tool filters is exactly
        the vendor semantics nothing here executes (open class L2) -- so the
        gate/write pair stays cold, deliberately.

        It also needs the accumulated context to be under a ceiling. A warm
        chain saves re-derivation for two or three hops and then inverts: a
        turn re-sends the whole context, so a session that has grown past the
        ceiling costs more per turn than a cold hop pays in total.
        """
        fresh = str(uuid.uuid4())
        if not getattr(self.cfg, "warm", False):
            return fresh, False
        key = (role, runtime, capability)
        with self._state_lock:
            live = self._warm_chains.get(key)
        if not live:
            return fresh, False
        if live.get("context", 0) > WARM_CONTEXT_CEILING:
            self.log(
                "warm chain %s/%s broken: context %s over ceiling"
                % (role, runtime, live.get("context"))
            )
            with self._state_lock:
                self._warm_chains.pop(key, None)
            return fresh, False
        return str(live["session"]), True

    def _note_warm_hop(
        self, role: str, runtime: str, capability: str, result: Result
    ) -> None:
        """Record or drop this role+runtime+capability chain after a hop."""
        if not getattr(self.cfg, "warm", False):
            return
        key = (role, runtime, capability)
        with self._state_lock:
            if not result.success or not result.session_id:
                # A failed hop leaves a session in an unknown state. The next
                # hop starts cold rather than inheriting it.
                self._warm_chains.pop(key, None)
                return
            usage = result.usage
            context = 0
            if usage is not None:
                context = (usage.input_tokens or 0) + (
                    usage.cache_read_input_tokens or 0
                )
                turns = result.num_turns or 0
                if turns:
                    # cache_read is context summed over turns; one turn's worth
                    # is the best available estimate of what the session holds.
                    context = int(context / turns)
            self._warm_chains[key] = {
                "session": result.session_id,
                "context": context,
            }

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
        with self._state_lock:
            prev = self.state.last_review if isinstance(self.state.last_review, dict) else {}
            attempt = int(prev.get("attempt") or 0) + 1
            self.state.last_review = {"attempt": attempt, "results": []}
            self.save()

    def _record_usage(
        self,
        role: str,
        phase: str,
        runtime: str,
        result: Result,
        *,
        prompt_bytes: Optional[int] = None,
        listed_bytes: Optional[int] = None,
    ) -> None:
        """Persist provider spend for this hop. Missing $ is logged, not dropped."""
        rec = usage_mod.hop_record(
            slug=self.state.slug,
            phase=phase,
            role=role,
            runtime=runtime,
            session_id=result.session_id,
            success=result.success,
            num_turns=result.num_turns,
            usage=result.usage,
            prompt_bytes=prompt_bytes,
            listed_bytes=listed_bytes,
        )
        usage_mod.record_hop(self.work, rec)
        self.log(usage_mod.format_hop_console(rec))
        hops = usage_mod.load_hops(self.work)
        if hops:
            self.log(usage_mod.format_summary_line(usage_mod.summarize(hops)))

    def _record_review_result(self, path: Path, *, num_turns: Optional[int] = None) -> None:
        with self._state_lock:
            rec = self.state.last_review if isinstance(self.state.last_review, dict) else {}
            results = [row for row in as_list(rec.get("results")) if isinstance(row, dict)]
            results = [row for row in results if row.get("name") != path.name]
            row = {
                "name": path.name,
                "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            if num_turns is not None:
                row["num_turns"] = num_turns
            results.append(row)
            rec = dict(rec)
            rec["results"] = results
            rec.setdefault("attempt", 1)
            self.state.last_review = rec
            self.save()

    def _refresh_recorded_review_digests(self) -> None:
        with self._state_lock:
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
                        **({"num_turns": row.get("num_turns")} if row.get("num_turns") is not None else {}),
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
                *self._inspect_only_lines(),
                self._listed_artifacts(
                    [
                        "brief.md",
                        "design.md",
                        "review.md",
                        "guardian.md",
                        "test-contract.md",
                    ]
                ),
                "Questions (max 10):",
                json.dumps(questions[:10], indent=2),
                "Answer from the questions and listed artifacts. "
                "Do not recensus the repository.",
            ],
        )
        result = self.invoke(
            target,
            "consult-%03d" % n,
            prompt,
            "answers.json",
            capability="read-only",
        )
        answers = as_str(result.output.get("answers_markdown")) or "(empty consult answers)"
        write_text(
            self.work / "consult" / ("%03d-%s-%s-answers.md" % (n, from_role, target)),
            answers,
        )
        return answers

    def _layout_blurb(self) -> str:
        code = (self.cfg.code_root or "").strip()
        extra = (
            "code_root='.' is the repository except test_root and git submodules. "
            if code == "."
            else "write-code also excludes test_root and git submodules that are not themselves code_root. "
        )
        if (self.cfg.test_root or "").strip() == ".":
            extra += (
                "test_root='.' is the repository except code_root and git submodules. "
            )
        return (
            "FOLDER FLEXIBILITY: Do not assume src/ or tests/ always exist. "
            "Discover the real repo layout. "
            "code_root=%r test_root=%r. "
            "%s"
            "If a root is missing, use an empty string and work with the actual tree. "
            "Never force creating both roots if the stack only needs one."
            % (self.cfg.code_root, self.cfg.test_root, extra)
        )

    def _submodule_paths(self) -> List[str]:
        if not gitutil.is_git_repo(self.repo):
            return []
        return gitutil.submodule_paths(self.repo)

    def _denied_write_code(self) -> List[str]:
        return denied_write_code_roots(
            self.cfg.code_root, self.cfg.test_root, self._submodule_paths()
        )

    def _denied_write_tests(self) -> List[str]:
        return denied_write_test_roots(
            self.cfg.code_root, self.cfg.test_root, self._submodule_paths()
        )

    def _code_write_lines(self) -> List[str]:
        lines = [
            "Edit ONLY under code_root=%r." % self.cfg.code_root,
            "NEVER edit tests (test_root=%r). Never weaken/skip/delete tests."
            % self.cfg.test_root,
        ]
        if (self.cfg.code_root or "").strip() == ".":
            lines.append(
                "code_root='.' is the repository except test_root and git submodules."
            )
        denied = self._denied_write_code()
        if denied:
            lines.append("Excluded write-code roots: %s." % ", ".join(denied))
        return lines

    def _architect_root_lines(self) -> List[str]:
        return [
            "code_root is the implementer write fence, not the package directory.",
            "Use '.' unless the work must be confined to one tree.",
            "'.' means the repository except test_root and git submodules.",
            "Name packages in structural_touchpoints, not as code_root.",
        ]

    def _adopt_design_roots(self, out: Dict[str, Any]) -> None:
        """Apply schema code_root/test_root unless project config locked them.

        state.json is a cache, not a second fence. A replan that names '.'
        must replace a leftover package path from an earlier hop.
        """
        code = normalize_root(as_str(out.get("code_root")))
        test = normalize_root(as_str(out.get("test_root")))
        if code and not self.cfg.lock_code_root:
            self.cfg.code_root = code
            self.state.code_root = code
        if test and not self.cfg.lock_test_root:
            self.cfg.test_root = test
            self.state.test_root = test

    def _inline_choice(self, sizes: Dict[str, int]) -> set:
        """Which listed artifacts travel in the prompt instead of as a path.

        Carrying S bytes inline costs S on every turn of the hop. Making the
        model fetch them costs S on every turn *after* the read, plus a whole
        extra turn -- and a turn re-sends the entire context, which is the
        expensive part (measured: ~54k tokens/turn, 20 turns/hop).

        The caps are deliberately small because that comparison has an
        unmeasured term: an agent can fetch several files in one turn, so N
        listed artifacts may cost one round trip rather than N. Under batching
        the two strategies converge, and inlining only stays ahead for
        artifacts small enough that carrying them is nearly free. A 4 KB file
        is ~1k tokens, ~20k over a whole hop, well under one turn's context;
        a 40 KB one would not be. Large dumps stay listed. prompt_bytes and
        listed_bytes in the ledger are what these numbers get tuned against.

        Small files are taken first because their ratio is best.
        """
        chosen = set()
        spent = 0
        for name, size in sorted(sizes.items(), key=lambda kv: (kv[1], kv[0])):
            if name in CROSS_ROLE_ARTIFACTS:
                continue
            if size > INLINE_ARTIFACT_MAX or spent + size > INLINE_TOTAL_MAX:
                continue
            chosen.add(name)
            spent += size
        return chosen

    def _listed_artifacts(self, names: List[str]) -> str:
        seen = set()
        ordered: List[str] = []
        for name in [CENSUS_ARTIFACT, *names]:
            if not name or name in seen:
                continue
            seen.add(name)
            ordered.append(name)
        sizes: Dict[str, int] = {}
        missing: List[str] = []
        for name in ordered:
            path = self.artifact(name)
            if path.is_file():
                sizes[name] = path.stat().st_size
            else:
                missing.append("- %s" % name)
        self._tls.listed_bytes = sum(sizes.values())
        inline = self._inline_choice(sizes)
        present = [
            "- %s" % self.artifact(name) for name in ordered if name in sizes and name not in inline
        ]
        lines = ["Work directory: %s" % self.work]
        for name in ordered:
            if name not in inline:
                continue
            lines.append("")
            lines.append("--- %s (inlined below; do not open it again) ---" % name)
            lines.append(self.read_artifact(name).rstrip("\n"))
            lines.append("--- end %s ---" % name)
        if inline:
            lines.append("")
        if present:
            lines.append("Read these files with tools before answering:")
            lines.extend(present)
        if missing:
            lines.append("Missing (n/a — do not open, do not invent):")
            lines.extend(missing)
        if self.artifact(CENSUS_ARTIFACT).is_file():
            lines.append(
                "census.md is a map (layout, where to look). "
                "It does not replace git/diff.patch, git/apply.patch, or the files. "
                "Do not recensus. Do inspect the listed diffs and the paths your task names."
            )
        else:
            lines.append(
                "census.md is missing. After you inspect, emit census_markdown: "
                "layout, missing layers, and verified path:line facts. "
                "Judgments stay in your role artifact. "
                "The orchestrator writes census.md once."
            )
        # Inlined artifacts are already in front of the model, but the tree is
        # not: a hop that answers from artifacts alone has reviewed nobody's
        # code. unfinished_inspect enforces the same rule from the other side.
        lines.append(
            "%sUse tools on the paths your task names, and on the tree itself. "
            "An inlined artifact is a claim, not the code. "
            "An empty or thin answer is valid only after that inspect."
            % ("Do not re-open anything inlined above. " if inline else "")
        )
        return "\n".join(lines)

    def _census_key(self) -> str:
        """What a cached census is a census *of*. Empty when unanswerable.

        HEAD alone: a census is a tree inventory, which is a property of the
        commit, not of the slug that paid for it. The dirty set rides in the
        sidecar rather than the key, because a working tree changes on every
        hop and keying on it would never hit.
        """
        if not gitutil.is_git_repo(self.repo):
            return ""
        return gitutil.head(self.repo) or ""

    def _census_cache(self, key: str) -> Path:
        return self.repo / ".team" / "census" / ("%s.md" % key)

    def _publish_census(self) -> None:
        """Copy this slug's census into the repo-wide cache.

        ``.team/census`` is deliberately **not** fence-exempt: it is durable
        and repo-wide, so anything a hop could leave there would be an input
        to every later run's prompts. The write is declared instead, so the
        fence bills the hop for what the hop wrote and not for this.
        """
        key = self._census_key()
        if not key:
            return
        text = self.read_artifact(CENSUS_ARTIFACT)
        if not text.strip():
            return
        cached = self._census_cache(key)
        if cached.is_file():
            return
        # Sidecar first: a reader that finds the map without it refuses to
        # reuse, so the incomplete state is the safe one.
        dump_json(
            cached.with_suffix(".json"),
            {
                "head": key,
                "slug": self.state.slug,
                "entries": self._census_entries(),
            },
        )
        write_text(cached, text)
        self._note_orchestrator_write(cached)
        self._note_orchestrator_write(cached.with_suffix(".json"))

    def _census_entries(self) -> Dict[str, str]:
        """Content ids of the dirty tree this census describes."""
        snap = gitutil.snapshot(self.repo)
        entries = dict(snap.get("entries") or {})
        return {
            rel: cid
            for rel, cid in entries.items()
            if rel in set(gitutil.product_paths(entries.keys()))
        }

    def _seed_census(self) -> None:
        """Reuse this repo state's census instead of buying it again.

        census.md was per-slug, so every feature and every review paid an
        inspect hop to re-derive the same tree map. Under .team/census it is
        written once per commit and read by every later run. Nothing is
        assumed about the dirty tree: paths that moved since the census was
        written are named, so a reused map is never stale-in-a-lying-way.
        """
        if self.artifact(CENSUS_ARTIFACT).is_file():
            return
        key = self._census_key()
        if not key:
            return
        cached = self._census_cache(key)
        sidecar = cached.with_suffix(".json")
        # No sidecar means no record of the tree it describes, so nothing can be
        # said about what moved. Buy a fresh census rather than reuse a map
        # whose staleness is unknowable.
        if not cached.is_file() or not sidecar.is_file():
            return
        text = cached.read_text(encoding="utf-8")
        # Content ids, not a path list. A path dirty when the census was
        # written and still dirty now, with entirely different bytes, is in
        # both path sets and would never be named -- the map would assert a
        # fact about a file it has never seen. gitutil.snapshot already
        # derives these and the fence already trusts them.
        stamped = dict(load_json(sidecar).get("entries") or {})
        now = self._census_entries()
        moved = sorted(
            rel
            for rel in set(now) | set(stamped)
            if now.get(rel) != stamped.get(rel)
        )
        if moved:
            text = text.rstrip() + "\n\n## Changed since this census\n\n" + "".join(
                "- %s\n" % rel for rel in moved[:_CENSUS_MOVED_MAX]
            )
            if len(moved) > _CENSUS_MOVED_MAX:
                text += "- ... and %d more\n" % (len(moved) - _CENSUS_MOVED_MAX)
            text += (
                "\nThese paths differ from the tree this census describes. "
                "Read them; do not trust the map for them.\n"
            )
        self.write_artifact(CENSUS_ARTIFACT, text)
        self.log(
            "census reused from %s (%d path(s) changed since)" % (cached, len(moved))
        )

    def _adopt_census(self, output: Any) -> None:
        """First inspect hop to emit census_markdown writes census.md. Later hops read it."""
        if not isinstance(output, dict):
            return
        text = as_str(output.get("census_markdown")).strip()
        if not text:
            return
        path = self.artifact(CENSUS_ARTIFACT)
        if path.is_file():
            return
        self.write_artifact(CENSUS_ARTIFACT, text)
        self.log("census written")
        self._publish_census()

    def _engineering_rules(self) -> str:
        path = engine_root() / "docs" / "engineering.md"
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return ""

    def _findings_prompt_lines(
        self,
        items: List[Dict[str, Any]],
        related: Optional[List[Dict[str, Any]]] = None,
    ) -> List[str]:
        lines = ["Findings:\n" + json.dumps(items, indent=2)]
        if related:
            lines.append(
                "Related guardian (context only; do not apply):\n"
                + json.dumps(list(related), indent=2)
            )
        return lines

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
        return gitutil.snapshot(self.repo)

    def _snapshot_for_restore(self) -> dict:
        """Hop-start tree plus extra-worktree watch. Fence + restore share this."""
        snap = dict(self._snapshot())
        snap["blobs"] = gitutil.worktree_blobs(self.repo)
        snap["outside"] = gitutil.outside_snapshot(self.repo)
        snap["outside_blobs"] = gitutil.outside_blobs(self.repo)
        return snap

    def _run_start_entries(self, phase_before: dict) -> dict:
        """Content ids dirty when this run began. Missing start falls back to hop start."""
        start = (self.state.git or {}).get("start")
        if isinstance(start, dict) and ("entries" in start or "paths" in start):
            return dict(start.get("entries") or {})
        return dict(phase_before.get("entries") or {})

    def _note_orchestrator_write(self, path: Path) -> None:
        """Record a path this process wrote, with the bytes it wrote.

        The fence's question is "did the *hop* write outside its roots". A
        sibling reviewer thread's window is open while orchestrator code
        writes the census cache, so without this the orchestrator's own write
        is billed to whichever hop happens to be running. The alternative --
        exempting the path by name -- would make it hop-writable, and a
        durable repo-wide path a hop can write is an input to every later run.

        Content id, not just the path: a hop that later changes those bytes
        no longer matches and is a violation again. Writing byte-identical
        content is the only way to be forgiven, which changes nothing.
        """
        try:
            rel = posix(str(path.resolve().relative_to(self.repo.resolve())))
        except (OSError, ValueError):
            return
        with self._state_lock:
            self._orchestrator_writes[rel] = gitutil.content_id(self.repo, rel)

    def _drop_orchestrator_writes(self, delta: List[str], after: dict) -> List[str]:
        """Remove paths this process wrote and the hop left alone."""
        with self._state_lock:
            mine = dict(self._orchestrator_writes)
        if not mine:
            return delta
        entries = dict(after.get("entries") or {})
        return [
            rel
            for rel in delta
            if not (rel in mine and entries.get(rel) == mine[rel])
        ]

    def _verify_write(
        self,
        phase: str,
        allowed: List[str],
        before: Any,
        *,
        denied: Optional[List[str]] = None,
    ) -> None:
        if isinstance(before, dict):
            before_snap = before
        else:
            before_snap = {"head": "", "paths": list(before or []), "entries": {}}
        after = self._snapshot()
        delta = gitutil.changed_paths(self.repo, before_snap, after)
        delta = self._drop_orchestrator_writes(delta, after)
        work_root = ".team/work/%s" % self.state.slug
        roots = explicit_roots(allowed)
        denied_roots = explicit_roots(denied or [])
        ok, bad = gitutil.verify_delta(
            delta,
            roots,
            always_allowed=[work_root, *gitutil.PROTOCOL_ROOTS],
            denied_roots=denied_roots,
        )
        # Extra-worktree is a different space than in-repo membership.
        # under_root('.', p) is total for relative paths — do not route
        # ../vibe.rc through allowed '.'. Any extra-worktree mutation is bad.
        outside_delta: List[str] = []
        if "outside" in before_snap:
            outside_delta = gitutil.outside_changed(
                before_snap.get("outside") or {},
                gitutil.outside_snapshot(self.repo),
            )
            for path in outside_delta:
                if path not in delta:
                    delta.append(path)
                if path not in bad:
                    bad.append(path)
        dirty = gitutil.already_dirty_mutations(
            delta,
            self._run_start_entries(before_snap),
            dict(before_snap.get("entries") or {}),
            dict(after.get("entries") or {}),
            exempt_roots=(work_root, *gitutil.PROTOCOL_ROOTS),
        )
        gitutil.write_path_list(self.work / "git" / ("after-%s.txt" % phase), delta)
        report = gitutil.describe_verify(
            phase,
            delta,
            bad,
            roots,
            head_before=str(before_snap.get("head") or ""),
            head_after=str(after.get("head") or ""),
            already_dirty=dirty,
            denied_roots=denied_roots,
        )
        write_text(self.work / "git" / ("verify-%s.md" % phase), report)
        head_changed = bool(
            before_snap.get("head")
            and after.get("head")
            and before_snap.get("head") != after.get("head")
        )
        if not roots and not outside_delta:
            self.log("git verify %s: no root set, advisory only (%d paths)" % (phase, len(delta)))
            return
        if bad:
            if denied_roots:
                msg = (
                    "%s wrote outside allowed roots %s (denied %s): %s"
                    % (phase, roots, denied_roots, ", ".join(bad))
                )
            else:
                msg = (
                    "%s wrote outside allowed roots %s: %s"
                    % (phase, roots, ", ".join(bad))
                )
            try:
                gitutil.revert_product(self.repo, before_snap)
                gitutil.revert_outside(self.repo, before_snap)
            except gitutil.GitError as exc:
                raise PipelineError("%s (restore failed: %s)" % (msg, exc)) from exc
            raise PipelineError(msg)
        if head_changed:
            self.log(
                "git verify %s: HEAD changed %s -> %s"
                % (phase, before_snap.get("head"), after.get("head"))
            )
        elif not ok:
            self.log("git verify %s: no new paths (continuing)" % phase)
        else:
            self.log("git verify %s: %d path(s) ok" % (phase, len(ok)))

    def _verify_write_code(self, phase: str, before: Any) -> None:
        self._verify_write(
            phase,
            [self.cfg.code_root],
            before,
            denied=self._denied_write_code(),
        )

    def _verify_write_tests(self, phase: str, before: Any) -> None:
        self._verify_write(
            phase,
            [self.cfg.test_root],
            before,
            denied=self._denied_write_tests(),
        )

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

    def _no_suite_evidence(self) -> str:
        """Reason the diagnose/repair rail has nothing to work from, or ''.

        PASS, FAIL and UNVERIFIED are three outcomes, not two. Skipping the rail
        only on PASS routes UNVERIFIED -- no test command discovered, collection
        death, a timeout -- into the debugger, which then names an owner, which
        un-skips repair, and an implementer rewrites production to fix a failure
        the host never observed. Only a proved product FAIL is evidence.
        """
        status = as_str((self.state.final or {}).get("status")) or "UNVERIFIED"
        if testhost.is_product_fail(self.state.final):
            return ""
        if status == "PASS":
            return ""
        return "suite %s (no failure observed)" % status

    def _skip_reason(self, phase: str) -> str:
        # config.OPTIONAL_PHASES is the one home; --skip is validated against
        # it, so this is a membership test, not a second list.
        if self.should_skip(phase) and phase in OPTIONAL_PHASES:
            return "requested"
        if phase == "debugger":
            if self._tests_passed():
                return "tests passed"
            unverified = self._no_suite_evidence()
            if unverified:
                return unverified
        if phase == "repair":
            if self._tests_passed():
                return "tests passed"
            unverified = self._no_suite_evidence()
            if unverified:
                return unverified
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
                *self._architect_root_lines(),
                "Return design_markdown plus code_root, test_root, acceptance_criteria,",
                "structural_touchpoints, and invariants. No function bodies.",
            ],
        )
        result = self.invoke("architect", "architect", prompt, "design.json")
        out = result.output
        design = as_str(out.get("design_markdown")) or "(empty design)"
        self.write_artifact("design.md", design)
        self._adopt_design_roots(out)
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
        result = self.invoke("architect", "architect-revise", prompt, "design.json")
        out = result.output
        design = as_str(out.get("design_markdown")) or self.read_artifact("design.md")
        self.write_artifact("design.md", design)
        self._adopt_design_roots(out)

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
                "tdd-design", "tdd-design-write", prompt, "tdd_design.json"
            )
            out = result.output
        contract = as_str(out.get("test_contract_markdown")) or "(empty test contract)"
        self.write_artifact("test-contract.md", contract)
        self.log("test contract written")

    def phase_test_writer(self) -> None:
        prompt = self._prompt(
            "test-writer",
            [
                self._listed_artifacts(
                    ["brief.md", "design.md", "test-contract.md", "tdd-summary.md"]
                ),
                "CONSULT GATE ONLY. Do not write files yet.",
                "If clear enough, ready=true, consult=\"none\", questions=[].",
                "If blocked, ready=false, consult one of implementer|tdd-design|architect,",
                "max 10 questions.",
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
            write_verify=lambda before: self._verify_write_tests(
                "test-writer", before
            ),
        )
        summary = as_str(result.output.get("summary")) or "(no tdd summary)"
        self.write_artifact("tdd-summary.md", summary)

    def phase_baseline(self) -> None:
        cmd = testhost.discover_test_command(
            self.repo, self.cfg.test_command, test_root=self.cfg.test_root
        )
        self.cfg.test_command = cmd
        run = testhost.run_suite(self.repo, cmd, timeout=self.cfg.phase_timeout)
        self.state.baseline = run
        self.write_artifact("baseline-report.md", testhost.render_report("Baseline test run", run))
        self.log("baseline %s (exit=%s)" % (run["status"], run["exit"]))

    def _apply_record_baseline(self) -> None:
        """This-apply pre-writer suite. Drops a stale prior-apply ``final``."""
        self.state.final = {}
        self.phase_baseline()

    def _run_apply_suite(self) -> Dict[str, Any]:
        cmd = testhost.discover_test_command(
            self.repo, self.cfg.test_command, test_root=self.cfg.test_root
        )
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
        return run

    def _present_artifacts(self, names: List[str]) -> List[str]:
        return [name for name in names if self.artifact(name).is_file()]

    def _apply_surface_artifacts(self) -> List[str]:
        return self._present_artifacts(
            [
                "git/apply.patch",
                "git/apply-names.txt",
                "apply-test-report.md",
                "apply-impl-summary.md",
                "apply-tdd-summary.md",
                "design-replan.md",
                "baseline-report.md",
            ]
        )

    def _write_apply_surface(self) -> None:
        """Uncommitted product delta vs HEAD. Range review includes this dirty tree."""
        if not gitutil.is_git_repo(self.repo):
            self.write_artifact("git/apply.patch", "(empty apply tree)\n")
            gitutil.write_path_list(self.work / "git" / "apply-names.txt", [])
            return
        dirty = gitutil.porcelain_paths(self.repo)
        # Sections carry their -z name, so the budget can drop bytes without
        # dropping the path from apply-names.txt. The fence still sees every
        # dirty path: porcelain_paths above is untouched by any of this.
        sections = gitutil.worktree_diff_sections(self.repo, dirty)
        patch, omitted = gitutil.budget_sections(
            sections,
            total=self.cfg.diff_budget,
            prefer=[self.cfg.code_root, self.cfg.test_root],
        )
        note = gitutil.budget_note(
            omitted, names_file="git/apply-names.txt", total=self.cfg.diff_budget
        )
        self.write_artifact("git/apply.patch", note + (patch or "(empty apply tree)\n"))
        names = [rel for rel, _text in sections] or gitutil.product_paths(dirty)
        gitutil.write_path_list(self.work / "git" / "apply-names.txt", names)
        if omitted:
            self.log(
                "apply surface: %d file(s) over the %d-byte budget, named in "
                "git/apply-names.txt" % (len(omitted), self.cfg.diff_budget)
            )
        range_md = self.read_artifact("range.md")
        if range_md and "## Apply working tree" not in range_md:
            extra = [
                "",
                "## Apply working tree",
                "",
                "%d product path(s) dirty vs HEAD. Empty tag..HEAD does not mean apply changed nothing."
                % len(names),
                "",
                "See `git/apply.patch` (authoritative for uncommitted apply work).",
                "",
            ]
            self.write_artifact("range.md", range_md.rstrip() + "\n" + "\n".join(extra))

    def phase_implementer(self) -> None:
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
                "WRITE PRODUCTION CODE NOW.",
                *self._code_write_lines(),
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
            write_verify=lambda before: self._verify_write_code("implementer", before),
        )
        summary = as_str(result.output.get("summary")) or "(no impl summary)"
        self.write_artifact("impl-summary.md", summary)

    def phase_final_test(self) -> None:
        cmd = testhost.discover_test_command(
            self.repo, self.cfg.test_command, test_root=self.cfg.test_root
        )
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
        if not is_model_runtime(self.cfg.assignment("tester")):
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

    def phase_debugger(
        self,
        *,
        seq_applied: Optional[List[Dict[str, Any]]] = None,
        rail: str = "feature",
    ) -> None:
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
        impl = "apply-impl-summary.md" if rail == "apply" else "impl-summary.md"
        suite = [self._host_suite_report(rail), impl]
        prompt = self._prompt(
            "debugger",
            [
                self._listed_artifacts(
                    ["design.md", "test-contract.md", "baseline-report.md"] + suite
                ),
                "Tests failed. Diagnose root cause.",
                *self._inspect_only_lines(),
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

    def phase_repair(self, *, rail: str = "feature") -> None:
        owner = self.state.diagnosis_owner or "implementer"
        suite = self._host_suite_report(rail)
        if owner == "test-writer":
            prompt = self._prompt(
                "test-writer",
                [
                    self._listed_artifacts(
                        ["design.md", "test-contract.md", "diagnosis.md", suite]
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
                write_verify=lambda before: self._verify_write_tests(
                    "repair", before
                ),
            )
            self.write_artifact(
                "repair-summary.md",
                as_str(result.output.get("summary")) or "(no repair summary)",
            )
        else:
            prompt = self._prompt(
                "implementer",
                [
                    self._listed_artifacts(
                        ["design.md", "test-contract.md", "diagnosis.md", suite]
                    ),
                    "REPAIR. Diagnosis says production is wrong. Fix production only.",
                    *self._code_write_lines(),
                    "Return summary and paths_touched.",
                ],
            )
            result = self.invoke(
                "implementer",
                "repair-implementer",
                prompt,
                "write_summary.json",
                capability="write-code",
                write_verify=lambda before: self._verify_write_code("repair", before),
            )
            self.write_artifact(
                "repair-summary.md",
                as_str(result.output.get("summary")) or "(no repair summary)",
            )
        self.log("repair via %s" % owner)

    def _host_suite_report(self, rail: str) -> str:
        """Living host-suite name. Feature and apply are different artifacts."""
        return "apply-test-report.md" if rail == "apply" else "test-report.md"

    def phase_verify_test(self, *, rail: str = "feature") -> None:
        cmd = testhost.discover_test_command(
            self.repo, self.cfg.test_command, test_root=self.cfg.test_root
        )
        self.cfg.test_command = cmd
        run = testhost.run_suite(self.repo, cmd, timeout=self.cfg.phase_timeout)
        comparison = testhost.compare(self.state.baseline, run)
        run = dict(run)
        run["comparison"] = comparison
        self.state.final = run
        md = testhost.render_report("Verify test run (after repair)", run, comparison)
        md = self._maybe_tester_agent(md, cmd, run, "tester-verify")
        self.write_artifact("verify-test-report.md", md)
        self.write_artifact(self._host_suite_report(rail), md)
        self.log("verify %s verdict=%s" % (run["status"], comparison["verdict"]))

    def phase_adversarial(self) -> None:
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
            write_verify=lambda before: self._verify_write_tests(
                "adversarial", before
            ),
        )
        md = as_str(result.output.get("adversarial_markdown")) or json.dumps(
            result.output, indent=2
        )
        self.write_artifact("adversarial.md", md)
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
        cmd = testhost.discover_test_command(
            self.repo, self.cfg.test_command, test_root=self.cfg.test_root
        )
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
        runtimes = expand_reviewer(self.cfg.assignment("reviewer"))
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
        apply_files = self._apply_surface_artifacts()
        artifacts.extend(apply_files)

        def one(runtime: str) -> Result:
            prompt = self._prompt(
                "reviewer",
                [
                    self._listed_artifacts(artifacts),
                    *self._inspect_only_lines(),
                    "Inspect the actual files and git status.",
                    "Summaries are claims, not evidence.",
                    *(
                        [
                            "git/apply.patch is the uncommitted apply delta.",
                            "An empty commit range does not mean nothing changed.",
                        ]
                        if apply_files
                        else []
                    ),
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
            with ThreadPoolExecutor(max_workers=len(runtimes)) as pool:
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

    def _fence_after_invoke(
        self,
        cap: str,
        phase: str,
        before: Any,
        write_verify: Optional[Callable[[Any], None]] = None,
    ) -> None:
        """Verify+restore for one invoke. invoke() is the only caller."""
        if not may_write(cap):
            self._fence_readonly(phase, before)
            return
        if write_verify is not None:
            write_verify(before)
            return
        if cap == "write-tests":
            self._verify_write_tests(phase, before)
            return
        self._verify_write_code(phase, before)

    def _fence_readonly(self, phase: str, before: Any) -> None:
        """Verify+restore for one read-only invoke. invoke() is the only caller."""
        after = self._snapshot()
        head_changed = bool(
            before.get("head")
            and after.get("head")
            and before.get("head") != after.get("head")
        )
        try:
            self._verify_write(phase, list(gitutil.PROTOCOL_ROOTS), before)
            if head_changed:
                raise PipelineError(
                    "%s changed HEAD %s -> %s (read-only hop must not commit)"
                    % (phase, before.get("head"), after.get("head"))
                )
        except PipelineError:
            self._restore_inspect_product(before)
            raise

    def _restore_inspect_product(self, before: Any) -> None:
        """Revert product-tree and extra-worktree writes. Skip .team/work."""
        if not isinstance(before, dict):
            before = {"head": "", "paths": list(before or []), "entries": {}, "blobs": {}}
        try:
            gitutil.revert_product(self.repo, before)
            gitutil.revert_outside(self.repo, before)
        except gitutil.GitError as exc:
            raise PipelineError("inspect restore failed: %s" % exc) from exc

    def _write_audit_report(self) -> None:
        status = self.read_artifact("status.md") or "(no status)"
        review = self.read_artifact("review.md") or "(no review yet)"
        combined = "# Status\n\n%s\n\n# Review\n\n%s\n" % (status.rstrip(), review.rstrip())
        self.write_artifact("report.md", combined)

    def phase_scout(self) -> None:
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
                *self._inspect_only_lines(),
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
        self.log("scout %d component(s)" % len(components))

    def phase_assess(self) -> None:
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
        self.log("status written")

    def phase_status_reviewer(self) -> None:
        self._begin_review_attempt()
        runtimes = expand_reviewer(self.cfg.assignment("reviewer"))
        artifacts = ["brief.md", "scout.md", "scout.json", "status.md"]

        def one(runtime: str) -> Result:
            prompt = self._prompt(
                "reviewer",
                [
                    self._listed_artifacts(artifacts),
                    "You are the adversarial reviewer on a STATUS audit.",
                    *self._inspect_only_lines(),
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

        parts = []
        if len(runtimes) == 1:
            ordered = runtimes
            results = [one(runtimes[0])]
        else:
            with ThreadPoolExecutor(max_workers=len(runtimes)) as pool:
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
        self.log("audit review written (%s)" % ", ".join(runtimes))
        self._log_items(findings_mod.collect_review_findings(self.work))

    def phase_range_reviewer(self) -> None:
        self._begin_review_attempt()
        runtimes = expand_reviewer(self.cfg.assignment("reviewer"))
        artifacts = [
            "brief.md",
            "range.md",
            "git/log.txt",
            "git/names.txt",
            "git/diff.patch",
            *[dest for _src, dest in RANGE_HEAD_LAW],
        ]
        apply_files = self._apply_surface_artifacts()
        artifacts.extend(apply_files)
        if apply_files:
            scope = [
                "RANGE REVIEW of the live dirty working tree, plus the collected commits.",
                "R is brief.md plus the live target AGENTS.md. git/committed-AGENTS.md is HEAD for comparison.",
                "census.md is a map. Still read git/diff.patch and git/apply.patch and the files.",
                "git/log.txt and git/names.txt are the commit set. git/apply.patch is uncommitted work vs HEAD.",
                "An empty git/diff.patch does not mean nothing changed.",
                "Summaries are claims.",
            ]
        else:
            scope = [
                "RANGE REVIEW of the live dirty working tree, plus the collected commits.",
                "R is brief.md plus the live target AGENTS.md. git/committed-AGENTS.md is HEAD for comparison.",
                "census.md is a map (where to look). Still read git/diff.patch and the files. Do not recensus.",
                "git/log.txt and git/names.txt are the commit set. Do not invent commits.",
                "This is not a PR-only review: the range may be 'since the last reviewed-* tag'.",
                "Inspect the listed diffs and the actual files in the working tree.",
            ]

        def one(runtime: str) -> Result:
            prompt = self._prompt(
                "reviewer",
                [
                    self._listed_artifacts(artifacts),
                    *self._inspect_only_lines(),
                    *scope,
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
            with ThreadPoolExecutor(max_workers=len(runtimes)) as pool:
                futs = [(rt, pool.submit(one, rt)) for rt in runtimes]
                ordered = [rt for rt, _ in futs]
                results = [fut.result() for _, fut in futs]
        for rt, result in zip(ordered, results):
            md = as_str(result.output.get("review_markdown")) or as_str(result.output.get("summary"))
            self.write_artifact("review-%s.md" % rt, md)
            parts.append((rt, result.output, md))
        merged = merge_reviews(parts)
        self.write_artifact("review.md", merged)
        self.log("range review written (%s)" % ", ".join(runtimes))
        self._log_items(findings_mod.collect_review_findings(self.work))
        if not self.artifact(CENSUS_ARTIFACT).is_file():
            raise PipelineError(
                "range reviewer did not emit census_markdown; later hops would recensus"
            )

    def phase_guardian(self) -> None:
        listed = [
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
        if self.state.mode == "range":
            listed.extend(
                [
                    "git/log.txt",
                    "git/names.txt",
                    *[dest for _src, dest in RANGE_HEAD_LAW],
                ]
            )
            identity = [
                "I is the live dirty working tree plus the collected commits.",
                "R is brief.md plus the live target AGENTS.md. "
                "git/committed-AGENTS.md is HEAD for comparison, not a substitute for the live file.",
                # The reviewer already read both patches end to end and wrote
                # census.md and review.md from them. Re-reading them here buys
                # the same bytes a second time; the guardian judges arrows, not
                # hunks. Named on demand, not listed for a mandatory full read.
                "census.md + review.md + git/names.txt are your map of the delta. "
                "git/diff.patch and git/apply.patch are on disk if a specific claim needs a hunk — "
                "open the paths you need, not the whole patch.",
                "Do not recensus. Do not restate a class already in review.md.",
            ]
            r_line = (
                "R = brief.md + live AGENTS.md. A = design.md. T = test-contract.md."
            )
        else:
            listed.append("git/apply.patch")
            identity = [
                "I = the tree. V = test/apply reports.",
                "review.md + census.md are the inspect when present. "
                "Do not recensus. Do not restate a class already in review.md.",
            ]
            r_line = "R = brief.md + target AGENTS.md. A = design.md. T = test-contract.md."
        prompt = self._prompt(
            "guardian",
            [
                self._listed_artifacts(listed),
                r_line,
                *identity,
                "Evaluate R→A, A→T, T→I, and I→R. The last arrow is required.",
                "A green suite does not prove I→R.",
                *self._inspect_only_lines(),
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
        try:
            self._log_items(findings_mod.collect_guardian_findings(self.work))
        except findings_mod.FindingsError as exc:
            raise PipelineError(str(exc)) from exc

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
        result = self.invoke("architect", "replan", prompt, "design.json")
        md = as_str(result.output.get("design_markdown")) or "(empty replan)"
        self.write_artifact("design-replan.md", md)
        self._adopt_design_roots(result.output)
        self.log("apply fence: code_root=%s test_root=%s" % (self.cfg.code_root, self.cfg.test_root))
        self.state.mark("replan")
        self.state.stop_reason = "replan"
        self.save()
        self.log("replan written to design-replan.md")

    def _apply_design_delta(self) -> str:
        delta = self.read_artifact("design-replan.md")
        if not delta:
            return ""
        living = merge_design_delta(self.read_artifact("design.md"), delta)
        self.write_artifact("design.md", living)
        # Fence follows the replan schema, not leftover state.json / layout.
        path = self.work / "prompts" / "replan.result.json"
        if path.is_file():
            try:
                data = load_json(path)
            except Exception:
                data = None
            if isinstance(data, dict):
                self._adopt_design_roots(data)
                self.save()
        return living

    def apply_replan(self) -> None:
        delta = self.read_artifact("design-replan.md")
        if not delta:
            raise PipelineError("apply-replan needs design-replan.md (run team replan first)")
        self._apply_design_delta()
        order = _phase_order(self.state)
        self.state.rewind_to("tdd-design", order)
        self.state.stop_reason = ""
        self.save()
        self.log("applied design-replan.md → design.md; resuming at tdd-design")
        self.run(start="tdd-design")

    def _inspect_only_lines(self) -> List[str]:
        return [
            "INSPECT ONLY. Do not create, edit, delete, or move any file.",
            "That includes AGENTS.md, README, docs, plans, tests, and production.",
            "Read/grep/list only. A write is a failed hop. Emit a finding instead.",
            "team apply owns the tree. Implementer owns repo docs when code_root is '.'",
        ]

    def _reviewer_finding_rules(self) -> str:
        return (
            "Each finding MUST set kind to one of: architecture, implementation, test, note.\n"
            "- architecture: design, invariants, boundaries — architect will replan\n"
            "- implementation: production bug — implementer will patch\n"
            "- test: missing/wrong tests or contract — tdd-design + test-writer\n"
            "- note: open class or non-actionable; listed only\n"
            "At most 10 findings (severity, title, evidence, path, kind).\n"
            "A finding is not a patch. Do not edit the tree to close a class.\n"
            "Do not emit the JSON object until tools have read the listed artifacts "
            "and you have inspected the files in scope. A progress finding is not a review."
        )

    def _write_followups(self, *, seq: Optional[Dict[str, Any]] = None) -> None:
        try:
            items = findings_mod.collect_all(self.work)
        except findings_mod.FindingsError as exc:
            raise PipelineError(str(exc)) from exc
        self.write_artifact(
            "followups.md",
            findings_mod.render_followups(items, seq=seq),
        )

    def _collect_apply_review_findings(self) -> List[Dict[str, Any]]:
        recorded = findings_mod._recorded_review_results(self.work)
        try:
            findings = findings_mod.collect_review_findings(self.work)
        except findings_mod.FindingsError as exc:
            extras = (
                findings_mod.unrecorded_reviewer_results(self.work, recorded)
                if recorded
                else []
            )
            msg = str(exc).lower()
            in_place = recorded and "digest mismatch" in msg and not extras
            if in_place:
                self._refresh_recorded_review_digests()
                try:
                    findings = findings_mod.collect_review_findings(self.work)
                except findings_mod.FindingsError as exc2:
                    raise PipelineError(str(exc2)) from exc2
            else:
                raise PipelineError(str(exc)) from exc
        if findings_mod.recorded_inspect_unfinished(self.work):
            raise PipelineError("recorded review is not a finished inspect")
        return findings

    def apply_review(
        self,
        *,
        dry_run: bool = False,
        seq: bool = False,
        skip_failed: bool = False,
        reopen: str = "",
        repair: bool = False,
    ) -> None:
        if self.state.mode == "audit":
            raise PipelineError("audit is read-only; apply needs a feature or range work slug")
        if not self.read_artifact("review.md"):
            raise PipelineError("apply needs review.md (run team review first)")

        findings = self._collect_apply_review_findings()
        findings = findings_mod.fill_missing_kinds(findings)
        try:
            guardian = findings_mod.collect_guardian_findings(self.work)
        except findings_mod.FindingsError as exc:
            raise PipelineError(str(exc)) from exc
        items = findings + guardian
        seq_state = findings_mod.load_seq_state(self.work)
        findings_mod.write_findings(self.work, items, seq=seq_state)
        self._write_followups(seq=seq_state)
        groups = findings_mod.group_by_kind(items)
        self.write_artifact("apply-plan.md", findings_mod.render_plan(groups))
        if findings_mod.needs_classify(findings, work=self.work) or groups.get(
            "unclassified"
        ):
            self.state.stop_reason = "needs-classification"
            self.save()
            self.log("apply: unclassified findings remain; run team review")
            return
        if seq:
            if reopen:
                self._seq_reopen(
                    reopen,
                    review_findings=findings,
                    guardian=guardian,
                    dry_run=dry_run,
                )
                return
            self._apply_seq(
                review_findings=findings,
                guardian=guardian,
                dry_run=dry_run,
                skip_failed=skip_failed,
                repair=repair,
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
                    suite_status="",
                    hops=[],
                ),
            )
            self.state.stop_reason = "applied"
            self.save()
            self.log("apply: nothing actionable")
            return

        self._apply_record_baseline()
        # A red baseline is not a reason to refuse: applying a fix for a bug
        # that fails the suite is the ordinary case. testhost.compare splits the
        # post-apply run into new vs preexisting failures against exactly this
        # record, so "what did the writer break" stays answerable. The run stops
        # later, at needs-repair, if the writers did not close it.
        hops.append(
            "baseline %s"
            % ((self.state.baseline or {}).get("status") or "UNVERIFIED")
        )

        if groups["architecture"]:
            self.replan()
            if self._apply_design_delta():
                hops.append("architect replan → design.md")
                self.log("applied design-replan.md → design.md")

        if groups["architecture"] or groups["test"]:
            kinds = ("architecture", "test") if groups["architecture"] else ("test",)
            self._apply_tdd_design(self._apply_items_for(items, *kinds))
            hops.append("tdd-design contract")
            self._apply_test_writer(self._apply_items_for(items, "test"))
            hops.append("test-writer")

        if groups["architecture"] or groups["implementation"]:
            impl_kinds = (
                ("architecture", "implementation")
                if groups["architecture"]
                else ("implementation",)
            )
            self._apply_implementer(self._apply_items_for(items, *impl_kinds))
            hops.append("implementer")

        run = self._run_apply_suite()
        hops.append("suite %s" % run.get("status"))
        self.log("apply-test %s" % run.get("status"))

        if testhost.needs_repair(run):
            if self._apply_repairs(repair=repair):
                try:
                    self.phase_debugger(rail="apply")
                    hops.append("debugger owner=%s" % (self.state.diagnosis_owner or "?"))
                    if self.state.diagnosis_owner in ("implementer", "test-writer"):
                        self.phase_repair(rail="apply")
                        self.phase_verify_test(rail="apply")
                        hops.append("repair + verify")
                        run = self.state.final or run
                except OptionalPhaseError as exc:
                    self._skip("debugger", str(exc))
                    hops.append("debugger skipped (%s)" % exc)
            else:
                hops.append("debug/repair off")
                self.log(self._repair_off_hint())

        if testhost.needs_repair(run):
            self.state.stop_reason = "needs-repair"
        else:
            self.state.stop_reason = "applied"

        self._write_apply_surface()
        self.write_artifact(
            "apply-summary.md",
            findings_mod.render_summary(
                groups,
                suite_status=str(run.get("status") or ""),
                hops=hops,
            ),
        )
        self.save()
        self.log("apply complete" if self.state.stop_reason == "applied" else "apply stopped: needs-repair")
        self._log_items(
            findings_mod.collect_all(self.work), sort=True, more_hint="followups.md"
        )

    def _seq_reopen(
        self,
        fid: str,
        *,
        review_findings: List[Dict[str, Any]],
        guardian: List[Dict[str, Any]],
        dry_run: bool = False,
    ) -> None:
        seq = findings_mod.load_seq_state(self.work)
        try:
            seq = findings_mod.reopen_prefix(seq, fid)
        except findings_mod.FindingsError as exc:
            raise PipelineError(str(exc))
        if dry_run:
            # --dry-run means "do not change what the next run will do". The
            # product tree is not the only such state: the seq queue is what the
            # next --seq consumes, and marking a suffix stale is a real edit to
            # it. Validate the reopen (reopen_prefix above refuses a bad id),
            # report it, change nothing.
            later = [
                row["id"]
                for row in findings_mod.latest_seq_rows(seq)
                if row.get("status") == "stale"
            ]
            self.state.stop_reason = "dry_run"
            self.save()
            self.log(
                "dry-run: would reopen %s; %d later class(es) would go stale%s"
                % (
                    fid,
                    len(later),
                    (" (%s)" % ", ".join(later)) if later else "",
                )
            )
            return
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
        dry_run: bool,
        skip_failed: bool,
        repair: bool = False,
    ) -> None:
        seq = findings_mod.load_seq_state(self.work)
        pool = list(review_findings) + list(guardian)
        # Drain markers whose class the pool no longer holds, and say so. The
        # queue reads them resolved either way; persisting keeps the ledger,
        # team list, and apply-seq.md from telling three different stories.
        resolved = findings_mod.resolve_seq_state(seq, pool)
        dropped = findings_mod.dropped_seq_markers(seq, resolved)
        if dropped:
            seq = resolved
            findings_mod.write_findings(self.work, review_findings + guardian, seq=seq)
            self.log(
                "seq: dropped %d marker(s) for class(es) no longer in the review: %s"
                % (len(dropped), ", ".join(dropped))
            )
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
            findings_mod.render_seq_plan(ranked, failed=seq.get("failed") or ""),
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

        applied_already = [
            row
            for row in findings_mod.latest_seq_rows(seq)
            if row.get("status") == "applied"
        ]
        if not applied_already:
            self._apply_record_baseline()

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
            ok = self._apply_one_seq(item, related, seq, repair=repair)
            seq = findings_mod.load_seq_state(self.work)
            if not ok:
                self._write_followups(seq=seq)
                self.write_artifact(
                    "apply-seq.md", findings_mod.render_seq_log(seq, slug=self.state.slug)
                )
                self._write_apply_surface()
                self.state.stop_reason = "seq-failed"
                self.save()
                self.log("apply --seq stopped: class %s failed" % item.get("id"))
                return
        seq = findings_mod.load_seq_state(self.work)
        self._write_followups(seq=seq)
        self.write_artifact("apply-seq.md", findings_mod.render_seq_log(seq, slug=self.state.slug))
        self._write_apply_surface()
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
        repair: bool = False,
    ) -> bool:
        fid = item.get("id") or findings_mod.finding_id(item)
        item = dict(item)
        item["id"] = fid
        seq_dir = self.work / "seq" / fid
        (seq_dir / "prompts").mkdir(parents=True, exist_ok=True)
        work_items = [item]
        write_text(seq_dir / "finding.json", json.dumps(work_items, indent=2))
        if related:
            related_rows = []
            for row in related:
                rec = dict(row)
                rec["id"] = findings_mod.finding_id(rec)
                related_rows.append(rec)
            write_text(seq_dir / "related.json", json.dumps(related_rows, indent=2))
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
                self._apply_replan_seq(work_items, related=related)
                hops.append("architect replan → design.md")
                self._apply_tdd_design(work_items, thin=True, related=related)
                hops.append("tdd-design contract")
                self._apply_test_writer(work_items, thin=True, related=related)
                hops.append("test-writer")
                self._apply_implementer(work_items, thin=True, related=related)
                hops.append("implementer")
            elif kind == "test":
                self._apply_tdd_design(work_items, thin=True, related=related)
                hops.append("tdd-design contract")
                self._apply_test_writer(work_items, thin=True, related=related)
                hops.append("test-writer")
            else:
                self._apply_implementer(work_items, thin=True, related=related)
                hops.append("implementer")

            run = self._run_apply_suite()
            hops.append("suite %s" % run.get("status"))
            self.log("seq-test %s" % run.get("status"))

            if testhost.needs_repair(run):
                if self._apply_repairs(repair=repair):
                    try:
                        applied_rows = [
                            row
                            for row in findings_mod.latest_seq_rows(seq)
                            if row.get("status") == "applied"
                        ]
                        self.phase_debugger(seq_applied=applied_rows, rail="apply")
                        hops.append(
                            "debugger owner=%s" % (self.state.diagnosis_owner or "?")
                        )
                        self._log_seq_disposition(seq, item)
                        if self.state.diagnosis_owner in ("implementer", "test-writer"):
                            self.phase_repair(rail="apply")
                            self.phase_verify_test(rail="apply")
                            hops.append("repair + verify")
                            run = dict(self.state.final or run)
                    except OptionalPhaseError as exc:
                        self._skip("debugger", str(exc))
                        hops.append("debugger skipped (%s)" % exc)
                else:
                    hops.append("debug/repair off")
                    self.log(self._repair_off_hint())

            suite = str((self.state.final or run).get("status") or run.get("status") or "")
            self._write_seq_checkpoint(seq_dir, item, start=start, hops=hops, suite=suite)
            self._seq_copy_artifacts(seq_dir)
            if testhost.is_product_fail(status=suite):
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
        patch = ""
        if gitutil.is_git_repo(self.repo):
            sections = gitutil.worktree_diff_sections(self.repo, touched)
            patch, omitted = gitutil.budget_sections(
                sections,
                total=self.cfg.diff_budget,
                prefer=[self.cfg.code_root, self.cfg.test_root],
            )
            patch = (
                gitutil.budget_note(
                    omitted,
                    names_file=str(seq_dir / "checkpoint.json") + " (touched)",
                    total=self.cfg.diff_budget,
                )
                + patch
            )
        if patch.strip():
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
                "status": status
                or ("failed" if testhost.is_product_fail(status=suite) else "applied"),
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

    def _apply_replan_seq(self, items: List[Dict[str, Any]], related: Optional[List[Dict[str, Any]]] = None) -> None:
        self._begin_hop("architect", "apply: architect questions")
        prompt = self._prompt(
            "architect",
            [
                self._listed_artifacts(["brief.md", "design.md", "apply-plan.md"]),
                "SEQ APPLY. Replan only this class. Do not read review.md.",
                *self._findings_prompt_lines(items, related),
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
                *self._findings_prompt_lines(items, related),
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
        result = self.invoke("architect", "replan", prompt, "design.json")
        md = as_str(result.output.get("design_markdown")) or "(empty replan)"
        self.write_artifact("design-replan.md", md)
        self._apply_design_delta()
        self._adopt_design_roots(result.output)
        self.log(
            "seq: applied design-replan.md → design.md; fence code_root=%s test_root=%s"
            % (self.cfg.code_root, self.cfg.test_root)
        )

    def _seq_delta_artifact(self, seq_dir: Path) -> List[str]:
        """The one patch a class hop needs: what this class changed.

        _write_seq_checkpoint already derives it from the class's own start
        and end snapshots. Listing it is what turns "inspect git status" --
        which an inspect hop has no terminal for -- into something the hop can
        actually do, and it is a fraction of the whole apply surface.
        """
        rel = seq_dir.relative_to(self.work) / "delta.patch"
        return [str(rel)] if (self.work / rel).is_file() else []

    def phase_seq_review(self, seq_dir: Path, items: List[Dict[str, Any]]) -> None:
        runtimes = expand_reviewer(self.cfg.assignment("reviewer"))
        artifacts = [
            "brief.md",
            "design.md",
            "test-contract.md",
            "apply-plan.md",
            "apply-impl-summary.md",
            "apply-tdd-summary.md",
            "apply-test-report.md",
            *self._seq_delta_artifact(seq_dir),
        ]

        def one(runtime: str) -> Result:
            prompt = self._prompt(
                "reviewer",
                [
                    self._listed_artifacts(artifacts),
                    "CLASS REVIEW. Review only the class that apply --seq just closed.",
                    "The original review.md is out of scope. Do not rewrite it.",
                    *self._inspect_only_lines(),
                    "The listed delta.patch is exactly what this class changed. "
                    "Read it and the files it names. You have no terminal.",
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
            with ThreadPoolExecutor(max_workers=len(runtimes)) as pool:
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
                        *self._seq_delta_artifact(seq_dir),
                    ]
                ),
                "CLASS GUARDIAN. Evaluate only this applied class.",
                *self._inspect_only_lines(),
                "Do not write guardian.md at the slug root.",
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

    def _apply_tdd_design(
        self,
        items: List[Dict[str, Any]],
        *,
        thin: bool = False,
        related: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        listed = ["brief.md", "design.md", "test-contract.md", "apply-plan.md"]
        if not thin:
            listed.insert(3, "review.md")
        prompt = self._prompt(
            "tdd-design",
            [
                self._listed_artifacts(listed),
                "APPLY REVIEW. Update the test contract so kind=test findings and any",
                "applied design delta are encoded. Do not write test or production files.",
                *self._findings_prompt_lines(items, related),
                "If a contract exists, write a revised full contract, not a fragment.",
                "ready must be true unless you have at most 10 questions for the architect.",
            ],
        )
        self._begin_hop("tdd-design", "apply: tdd-design")
        result = self.invoke(
            "tdd-design", "tdd-design-apply", prompt, "tdd_design.json"
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
                "tdd-design", "tdd-design-apply-write", prompt, "tdd_design.json"
            )
            out = result.output
        contract = as_str(out.get("test_contract_markdown")) or self.read_artifact(
            "test-contract.md"
        )
        if contract:
            self.write_artifact("test-contract.md", contract)
        self.log("apply: test contract updated")

    def _apply_items_for(
        self, items: List[Dict[str, Any]], *kinds: str
    ) -> List[Dict[str, Any]]:
        wanted = set(kinds)
        return [row for row in items if (row.get("kind") or "") in wanted]

    def _apply_test_writer(
        self,
        items: List[Dict[str, Any]],
        *,
        thin: bool = False,
        related: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        listed = ["brief.md", "design.md", "test-contract.md", "apply-plan.md"]
        if not thin:
            listed.insert(3, "review.md")
        prompt = self._prompt(
            "test-writer",
            [
                self._listed_artifacts(listed),
                "APPLY REVIEW. CONSULT GATE ONLY. Do not write files yet.",
                "Encode kind=test findings as tests. Production is the implementer's.",
                "If you need the production shape or a seam the implementer will own,",
                "ready=false, consult=\"implementer\".",
                "If the contract is unclear, consult tdd-design or architect.",
                "If clear, ready=true, consult=\"none\", questions=[].",
                *self._findings_prompt_lines(items, related),
            ],
        )
        self._begin_hop("test-writer", "apply: test-writer gate")
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
                as_str(gout.get("consult")) or "implementer",
                as_list(gout.get("questions")),
                "test-writer",
            )
        prompt = self._prompt(
            "test-writer",
            [
                self._listed_artifacts(listed),
                "APPLY REVIEW. Encode kind=test findings (and the current contract).",
                "Edit ONLY under test_root=%r. NEVER edit production (code_root=%r)."
                % (self.cfg.test_root, self.cfg.code_root),
                "Implementation findings are out of scope. Do not patch code_root.",
                *self._findings_prompt_lines(items, related),
                "Consult answers:\n" + answers,
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
            write_verify=lambda before: self._verify_write_tests(
                "apply-test-writer", before
            ),
        )
        self.write_artifact(
            "apply-tdd-summary.md",
            as_str(result.output.get("summary")) or "(no apply test summary)",
        )
        self.log("apply: tests")

    def _apply_implementer(
        self,
        items: List[Dict[str, Any]],
        *,
        thin: bool = False,
        related: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        listed = ["brief.md", "design.md", "test-contract.md", "apply-plan.md"]
        if not thin:
            listed.insert(3, "review.md")
        prompt = self._prompt(
            "implementer",
            [
                self._listed_artifacts(listed),
                "APPLY REVIEW. Patch kind=implementation findings and realize any",
                "applied design delta.",
                *self._code_write_lines(),
                *self._findings_prompt_lines(items, related),
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
            write_verify=lambda before: self._verify_write_code(
                "apply-implementer", before
            ),
        )
        self.write_artifact(
            "apply-impl-summary.md",
            as_str(result.output.get("summary")) or "(no apply impl summary)",
        )
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
    snap = gitutil.snapshot(repo)
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
        count = gitutil.oneline_commit_count(log)
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
    # Superset of the patch's paths: names.txt is the cheap map, so it carries
    # paths the deduped cumulative patch no longer shows (a file renamed or
    # deleted mid-range). One derivation, in gitutil. Read from the *uncapped*
    # patch on the PR rail -- capping first would hide exactly the paths the
    # budget note promises are listed here.
    names = gitutil.range_name_only(repo, base) if not pr else gitutil.paths_from_diff(diff)
    gitutil.write_path_list(work / "git" / "names.txt", names)
    # The one rail with only patch text; names.txt above is the complete list.
    diff, dropped = gitutil.budget_patch_text(diff, total=cfg.diff_budget)
    diff_note = gitutil.budget_note(
        count=dropped, names_file="git/names.txt", total=cfg.diff_budget
    )
    write_text(work / "git" / "diff.patch", diff_note + (diff or "(empty diff)\n"))
    for src, dest in RANGE_HEAD_LAW:
        blob = gitutil.committed_blob(repo, src)
        if blob.strip():
            write_text(work / dest, blob)
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
    pipe = Pipeline(cfg, state, work)
    pipe._write_apply_surface()
    return pipe


def load_pipeline(cfg: Config, slug: str) -> Pipeline:
    work = work_dir(cfg.repo, slug)
    if not (work / "state.json").is_file():
        raise PipelineError("no run at %s" % work)
    state = State.load(work)
    if not cfg.code_root:
        cfg.code_root = normalize_root(state.code_root)
    else:
        cfg.code_root = normalize_root(cfg.code_root)
    if not cfg.test_root:
        cfg.test_root = normalize_root(state.test_root)
    else:
        cfg.test_root = normalize_root(cfg.test_root)
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
