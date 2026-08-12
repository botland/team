# Artifact protocol

Every phase reads files and may write files. Chat context is not the handoff.

| File | Produced by | Trusted? |
|------|-------------|----------|
| `brief.md` | orchestrator from the CLI brief | yes |
| `design.md` | architect (orchestrator writes from schema) | source of truth for structure + invariants |
| `critic.md` | critic | claim about the design vs brief |
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
| `followups.md` | orchestrator | open classes from review + guardian |
| `diagnosis.md` | debugger | claim about a failure |
| `review-*.md` | each reviewer | independent; they must not read each other |
| `review.md` | orchestrator merge | human-facing |
| `guardian.md` | guardian | invariant risks after review |
| `design-replan.md` | architect replan | delta only |
| `state.json` | orchestrator | machine state |
| `consult/*.json` | orchestrator | questions + answers |
| `git/*` | orchestrator | fence evidence |
| `scout.md` / `scout.json` | scout (audit) | untrusted inventory |
| `status.md` | architect assess (audit) | status claims to verify |
| `report.md` | orchestrator (audit) | **primary human artifact for audit** (status + review) |

Reviewers and the guardian must read the repository and these files. They must not treat summaries as evidence.

Every phase prompt includes `docs/engineering.md` from the engine (class vs instance, seams, vacuous guards). That file is not copied into the target repo.
