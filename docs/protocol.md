# Artifact protocol

Every phase reads files and may write files. Chat context is not the handoff.

| File | Produced by | Trusted? |
|------|-------------|----------|
| `brief.md` | orchestrator from the CLI brief | yes |
| `design.md` | architect (orchestrator writes from schema) | source of truth for structure + invariants |
| `critic.md` | critic | adversarial: tries to kill the design (not help it) |
| `test-contract.md` | tdd-design | source of truth for intended tests |
| tests under `test_root` | test-writer | authoritative tests |
| `tdd-summary.md` | test-writer | untrusted claim |
| production under `code_root` | implementer | authoritative code |
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
| `apply-tdd-summary.md` | test-writer (apply) | untrusted claim |
| `apply-impl-summary.md` | implementer (apply) | untrusted claim |
| `apply-test-report.md` | orchestrator (apply) | suite after apply |
| `review-*.md` | each reviewer | independent; they must not read each other |
| `review.md` | orchestrator merge | human-facing |
| `guardian.md` | guardian | R→A→T→I and **I→R** (implementation vs original brief) |
| `design-replan.md` | architect replan | delta only |
| `state.json` | orchestrator | machine state |
| `consult/*.json` | orchestrator | questions + answers |
| `git/*` | orchestrator | fence evidence |
| `scout.md` / `scout.json` | scout (audit) | untrusted inventory |
| `status.md` | architect assess (audit) | status claims to verify |
| `report.md` | orchestrator (audit) | **primary human artifact for audit** (status + review) |
| `range.md` | orchestrator (range review) | scope: since `reviewed-*` tag, `--since`, or `--pr` |
| `git/log.txt` | orchestrator | commits in the range (authoritative) |
| `git/diff.patch` | orchestrator | diff for the range (authoritative) |

Reviewers and the guardian must read the repository and these files. They must not treat summaries as evidence.

Every phase prompt includes `docs/engineering.md` from the engine (class vs instance, seams, vacuous guards). That file is not copied into the target repo.
