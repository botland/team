# Working on this repo

This is the team orchestrator. It is not an application feature repo.

## Invariants

- Files are the protocol. Phases communicate through `.team/work/<slug>/` in the **target** repo.
- The orchestrator decides transitions. Agents return `ready` / questions / artifacts; they do not pause the host.
- Git diffs are the write fence. Do not trust `paths_touched`.
- Role → runtime assignment is data (`config.toml`, `--assign`). Do not hard-code Claude or Grok inside a phase except as a default in `ROLES`.
- Persona text has one home: `personas/*.md`.
- Do not write outside this repository when changing the engine (no `~/vibe.rc`, no `~/.team`, no target-app source).
- `feature` implements (including debugger repair, adversarial tests, and `replan --continue`). `audit` is scout → assess → review and must not write outside `.team/work/`.
- `team review` without a slug is a **commit-range** review (since last `reviewed-*` tag, else last tag, else the branch). `--pr` is optional. Do not assume reviews are PR-only.
- Past-commits review uses **one** reviewer (`review.range_reviewer`, default grok). PR and feature review use `roles.reviewer` (default both).
- `team apply <slug>` processes classified review findings (architecture → replan, test → tdd/test-writer, implementation → implementer). Unstructured findings trigger a re-review first.
- `team apply <slug> --seq` applies one class at a time in feature order (architecture → test → implementation, severity inside each kind) until failure or the queue is empty. Class reviews go to `seq/<id>/`; they must not overwrite `review.md`. On failure, stop, leave the tree, retry the same class or `--skip-failed`. `--reopen <id>` opens an earlier applied/failed class and marks the suffix stale. `team list` shows class ids and status.
- Manage the past-commits watermark with `team review --list-tags`, `--show-range`, `--mark [ref]`, `--delete-tag`, `--since`, and `--stamp`.
- Both CLIs run **headless**. Claude needs `-p`; Grok needs `--prompt-file` and `--no-alt-screen`. Do not add `--fullscreen` or drop `-p`.
- Repo-agnostic reasoning lives in `docs/engineering.md` and is injected into every agent prompt. Do not fork it into personas. Target-repo product law stays in that repo.

## Tests

`python3 -m unittest discover -s tests -v` (or `make test`). Pipeline tests use `--fake` and must not call Claude or Grok.

## Adding a role

1. Add the persona under `personas/`.
2. Register it in `src/team/config.py` `ROLES` with default runtime and allowed runtimes.
3. Add a schema if it has structured output.
4. Add a phase function and a `PHASE_ORDER` or `AUDIT_PHASE_ORDER` entry.
5. Extend `FakeRuntime` canned output.
6. Keep defaults aligned with “reasoning on Claude, execution on Grok” unless the role is explicitly flexible-only.
