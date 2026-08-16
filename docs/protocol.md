# Artifact protocol

Every phase reads files and may write files. Chat context is not the handoff. Each model hop starts a new session (`--session-id`); work artifacts are the only memory.

| File | Produced by | Trusted? |
|------|-------------|----------|
| `brief.md` | orchestrator from the CLI brief | yes |
| `design.md` | architect (orchestrator writes from schema) | source of truth for structure + invariants |
| `critic.md` | critic | adversarial: tries to kill the design (not help it) |
| `test-contract.md` | tdd-design | source of truth for intended tests |
| tests under `test_root` | test-writer | authoritative tests |
| `tdd-summary.md` | test-writer | untrusted claim |
| production under `code_root` (`.` = repo except `test_root` and git submodules) | implementer | authoritative code |
| `impl-summary.md` | implementer | untrusted claim |
| `baseline-report.md` | orchestrator | authoritative pre-impl suite |
| `test-report.md` | orchestrator (+ optional tester commentary) | authoritative post-impl suite |
| `adversarial.md` | adversarial | attack list |
| tests under `test_root` (attack) | adversarial | new failing/contract tests |
| `adversarial-test-report.md` | orchestrator | suite after attack tests |
| `diagnosis.md` | debugger | root cause; owner |
| `repair-summary.md` | implementer or test-writer | one repair hop |
| `verify-test-report.md` | orchestrator | suite after repair |
| `followups.md` | orchestrator | open classes from review + guardian (includes `kind`) |
| `findings.json` | orchestrator (apply) | classified findings used to route apply hops |
| `apply-plan.md` | orchestrator (apply) | what will be applied, by kind |
| `apply-summary.md` | orchestrator (apply) | hops taken + suite result |
| `apply-seq.md` | orchestrator (`apply --seq`) | one-class log; stop/retry/skip on failure |
| `seq/<id>/` | orchestrator (`apply --seq`) | finding, plan, suite (apply does not write class reviews) |
| `seq/<id>/review.md` | orchestrator (`team review --seq`) | class review; must not overwrite slug `review.md` |
| `seq/<id>/checkpoint.json` | orchestrator | trusted: heads, snapshots, touched paths, suite, assumptions |
| `seq/<id>/delta.patch` | orchestrator | product-file diff for that class |
| `seq/<id>/reopen.md` | orchestrator (`--reopen`) | which later ids went stale |
| `apply-tdd-summary.md` | test-writer (apply) | untrusted claim |
| `apply-impl-summary.md` | implementer (apply) | untrusted claim |
| `apply-test-report.md` | orchestrator (apply) | suite after apply |
| `review-*.md` | each reviewer | independent; they must not read each other |
| `review.md` | orchestrator merge | human-facing |
| `guardian.md` | guardian | R→A→T→I and **I→R** (implementation vs original brief) |
| `design-replan.md` | architect replan | delta only; apply / `replan --continue` merge it into `design.md` |
| `state.json` | orchestrator | machine state |
| `consult/*.json` | orchestrator | questions + answers |
| `git/*` | orchestrator | fence evidence (in-repo dirty / tree walk; extra-worktree violations listed on the verify report) |
| `scout.md` / `scout.json` | scout (audit) | untrusted inventory |
| `status.md` | architect assess (audit) | status claims to verify |
| `report.md` | orchestrator (audit) | **primary human artifact for audit** (status + review) |
| `census.md` | first inspect hop (orchestrator writes from `census_markdown`) | tree inventory: layout, missing layers, verified path:line facts. One writer. Later hops read it and do not recensus. Judgments stay in `review.md` / `guardian.md` / `design.md`. Audit still has `scout.md` (component inventory); `census.md` is the shared fact file. |
| `usage.jsonl` (slug) | orchestrator (one line per model hop) | this-run spend. Wiped by `--force`. Not an inspect input. |
| `.team/work/usage.jsonl` | orchestrator (append-only repo ledger) | durable spend across `--force`. `team costs` reads this. Missing `$` is unknown, never free. Not an inspect input. |
| `usage.md` | orchestrator (rewritten from the slug log) | human spend table + running total. Not an inspect input. |
| `range.md` | orchestrator (range review) | scope: since `reviewed-*` tag, `--since`, or `--pr` |
| `git/log.txt` | orchestrator | commits in the range (authoritative) |
| `git/names.txt` | orchestrator | paths those commits touched (the inventory) |
| `git/committed-AGENTS.md` | orchestrator (range) | product law at HEAD, for comparison. Live `AGENTS.md` is still `R` |
| `git/diff.patch` | orchestrator | collected commit dump (any size). Required reading. `census.md` does not replace it |
| `git/apply.patch` | orchestrator (range start, apply, and `team review` of an existing slug) | uncommitted product delta vs HEAD (the dirty tree) |
| `git/apply-names.txt` | orchestrator (apply, and `team review` of an existing slug) | paths in `git/apply.patch` |

Reviewers and the guardian must read the repository and these files. They must not treat summaries as evidence.

Every phase prompt includes `docs/engineering.md` from the engine (class vs instance, seams, vacuous guards). That file is not copied into the target repo.
