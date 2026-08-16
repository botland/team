# Working on this repo

This is the team orchestrator. It is not an application feature repo.

## Invariants

- Files are the protocol. Phases communicate through `.team/work/<slug>/` in the **target** repo. Each model hop is a new session; do not `--resume` a prior thread. `team resume` continues the pipeline, not the model. Tree inventory lives in `census.md` (written once). Later hops read it; they do not recensus. Judgments stay in `review.md` / `guardian.md` / `design.md`. Spend lives in `usage.jsonl` / `usage.md` (orchestrator; provider envelope only — omitted `$` is unknown, never free).
- The orchestrator decides transitions. Agents return `ready` / questions / artifacts; they do not pause the host.
- The write fence is role-root membership of every path a hop persisted — not `paths_touched`, and not git porcelain of `--repo` alone. In-repo: git dirty paths when the target is a git toplevel; the tree walk when it is not. Extra-worktree: the leaves product law names (`vibe.rc`, `.team`) at `--repo`'s parent and `$HOME` fail the hop and are restored. A directory leaf is the whole tree under that name, not only immediate files. Other extra-repo paths need a sandbox and are not this observer. `code_root='.'` is the repository except `test_root` and git submodules. Unset, empty, or whitespace `test_root` still names the conventional `tests/` tree for that exception (the same fallback testhost uses for discovery). `test_root='.'` is the repository except `code_root` and git submodules. A hop may edit files that were already dirty versus run start. Only `write-tests` / `write-code` may write (`may_write`). CLI `--deny` is an approximation. Every other persona — inspect (reviewer, guardian, architect, critic, tdd-design, debugger, scout, consults) and execute (tester) — has write tools denied and the inspect fence. Repo docs (`AGENTS.md`, README) are implementer work when `code_root` is `.`.
- Git output that names paths is read as `-z` records (`gitutil.git_records`), never parsed as lines: git quotes and escapes names in line output, `" -> "` is a legal substring of a filename, and the fence decides root membership from these strings. `git()` also sets `core.quotepath=false` for the one reader that has only patch text (`paths_from_diff`, the PR rail).
- Host suite is `test_command`. `test_root` is the write fence, not the suite path. A nested `test_root` does not invent cwd, `PYTHONPATH`, or selected dirs.
- Role → runtime assignment is data (`config.toml`, `--assign`). Runtimes are interchangeable adapters. Do not hard-code a vendor inside a phase except as a default in `ROLES`. Claude and Grok are the shipped pair; another headless terminal coding agent is a new `Runtime` + `register` (and `CODING_RUNTIMES` if it is a shipped peer), not a phase fork. `both` expands through `expand_reviewer`, not a second vendor list in each review phase.
- Role → effort is data (`[effort]`, `--effort ROLE=LEVEL`), a neutral `0`–`5`. Runtimes own their own rung names; `runners` maps a level to the nearest one each CLI has. Do not invent `grokxhigh` as a runtime.
- Adapters map one capability set, and `AdapterCapabilityParityTests` says so: the paths a hop is refused, whether it has a terminal, and whether it keeps the read tools must agree across every runtime. `write-tests` / `write-code` is not `execute`. Claude tool-filter flags are emitted once each, comma-joined; Claude's allow side only pre-approves under `acceptEdits`, so the git fence is the boundary there (`FOLLOWUPS.md` L1).
- `.team/config.toml` is read by `tomllib` and written by hand (comments and unknown keys survive). That pair is a seam: change either side with the round-trip contract test.
- `--skip` names an optional phase (`config.OPTIONAL_PHASES`) or it is an error. The flag, `TEAM_SKIP`, and `[run] skip` all resolve through `resolve_skip`; `_skip_reason` tests the same tuple.
- Persona text has one home: `personas/*.md`.
- Do not write outside this repository when changing the engine (no `~/vibe.rc`, no `~/.team`, no target-app source).
- `feature` implements (including debugger repair, adversarial tests, and `replan --continue`). It does not run reviewer or guardian. `team review <slug>` is the inspect. `audit` is scout → assess → review and must not write outside `.team/work/`.
- `team review` without a slug is a **commit-range** review (since last `reviewed-*` tag, else last tag, else the branch). `--pr` is optional. Do not assume reviews are PR-only.
- Reviewer can be `claude`, `grok`, or `both` (parallel) in every mode. Past-commits defaults to `review.range_reviewer` (grok). PR and feature review default to `roles.reviewer` (both).
- `team apply` without a slug uses the same default as unscoped `team review` (`review-since-tag`). `team apply <slug>` processes classified findings from review **and** guardian (architecture → replan, test → tdd/test-writer, implementation → implementer). Missing `design.md` is not a skip: replan writes A. Missing `test-contract.md` is not a skip: tdd-design writes T. Apply does not invoke reviewer or guardian. Unstructured findings stop apply (`needs-classification`); run `team review`. `--seq` uses that same queue, one class at a time. Debugger + repair are opt-in on this rail (`--repair`); by default a failing suite stops at `needs-repair` (or fails that `--seq` class) with no diagnose hop. That loop is headed for its own rail — do not make it automatic on apply again.
- `team apply <slug> --seq` applies one class at a time in feature order (architecture → test → implementation, severity inside each kind) until failure or the queue is empty. Apply does not write class reviews. Re-review a finished class with `team review <slug> --seq`; that review must not overwrite `review.md`. On failure, stop, leave the tree, retry the same class or `--skip-failed`. `--reopen <id>` opens an earlier applied/failed class and marks the suffix stale (temporary; not skipped). When that class is next applied or skipped, the suffix returns to the queue. Related guardian rows are context only and do not inherit applied. `team list` shows class ids and status.
- Manage the past-commits watermark with `team review --list-tags`, `--show-range`, `--mark [ref]`, `--delete-tag`, `--since`, and `--stamp`. "The last tag" has one ordering — newest reachable tag by creation date (`newest_reachable_tag`), for the `reviewed-*` pattern and the plain-tag fallback alike, because `--mark <ref>` stamps an arbitrary ref and a rewind is deliberate.
- A finding's class id is `(path, title, evidence)` — not `kind`, which the router owns and a re-review may change. The `--seq` ledger's pending markers (`resume` / `failed` / `stale`) are read through `resolve_seq_state`: a marker whose class the live pool no longer holds is not pending, so no ledger state is undrainable.
- Both CLIs run **headless**. Claude needs `-p`; Grok needs `--prompt-file` and `--no-alt-screen`. Do not add `--fullscreen` or drop `-p`.
- Repo-agnostic reasoning lives in `docs/engineering.md` and is injected into every agent prompt. Do not fork it into personas. Target-repo product law stays in that repo.

## Open classes

`FOLLOWUPS.md` is the register, and the only one. A row closes on a mechanism
at rung 1–2, never because an instance is gone; a contract test or a census
marks it **guarded**, which stays open. When you fix a defect, add or edit the
row in the same change. Do not start a second list.

## Tests

`python3 -m unittest discover -s tests -v` (or `make test`). Pipeline tests use `--fake` and must not call Claude or Grok.

## Adding a role

1. Add the persona under `personas/`.
2. Register it in `src/team/config.py` `ROLES` with default runtime and allowed runtimes (`CODING_RUNTIMES` unless the role is host-only or reviewer/`both`).
3. Add a schema if it has structured output.
4. Add a phase function and a `PHASE_ORDER` or `AUDIT_PHASE_ORDER` entry.
5. Extend `FakeRuntime` canned output.
6. Defaults may prefer one shipped adapter; every coding role must still accept the whole `CODING_RUNTIMES` set.

## Adding a runtime

1. Subclass `Runtime` and speak that CLI's headless argv (no TUI). Map the same capabilities (`read-only`, `write-tests`, `write-code`, `execute`).
2. Register it in `runners._REGISTRY`. If it is a shipped peer, add the name to `CODING_RUNTIMES` (roles and `both` follow). A one-off adapter can `register()` and be `--assign`ed without joining the shipped pair.
3. Do not add `if runtime == "…"` branches in `pipeline.py` or `cli.py`.
4. Spend, if the CLI emits it, is the shared envelope (`usage`, `total_cost_usd`) — not a vendor-specific parser in the phase.
