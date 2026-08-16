# Open classes

The register of **classes**, not of instances. A row closes on a named
mechanism at rung 1–2 (unrepresentable, or delegated to the layer that owns
the space); a census or a contract test marks it **guarded**, which is a state
that stays open. Never append a status line to a row — edit it or delete it.

Rungs: 1 unrepresentable · 2 delegate to the owning layer · 3 one authority +
census · 4 contract test across a seam · 5 review/doc/convention.

Round of 2026-08-16, against the review of `fd3021f` (`/tmp/review/claude`).

| # | Class | Rung reached | Mechanism | Open? |
|---|---|---|---|---|
| L1 | Claude tool filters cannot express an allowlist | 5 + boundary | Under `--permission-mode acceptEdits` an `Edit(root/**)` entry pre-approves; it does not refuse the rest. Grok's `--allow` does. The deny sets match (`write_tool_path_filters`, one function) and `AdapterCapabilityParityTests` pins that, but a path outside both role roots is reachable on Claude and refused on Grok. The git write fence is the boundary and catches it after the fact. | **open** — closes if the CLI grows a fail-closed allowlist for edits, or if the fence moves in front of the hop (sandbox) |
| L2 | Adapter argv is only tested against semantic doubles | 4 (across the doubles) | `tests/support/claude_argv.py` and `grok_argv.py` interpret argv the way we believe each CLI does. Nothing executes the real binaries, so a vendor changing flag semantics — union vs last-wins, what `acceptEdits` implies — is invisible here. Single-occurrence comma-joined flags remove the worst of it by construction. | **open** — needs a smoke hop against each CLI, outside the unit suite |
| L3 | Class identity contains the model's title | 5 | `finding_identity` is `(path, title, evidence)`; a re-worded finding is a new class. Nothing in a finding payload is more stable. The ledger no longer strands on it (`resolve_seq_state`), so the cost is a re-applied class, not a wedged queue. | **open as a class** |
| L4 | The seq ledger can hold a state no command produced | 2 | `findings.json` on disk is three sets plus two scalars; `resolve_seq_state` is the only reader and drops any marker whose class left the pool. The rung-1 shape is one `pending` slot resolved against the pool, which would make the bad state unwritable rather than unread. | **guarded** |
| L5 | One path list still comes from parsed text | 3 | Every git reader uses `-z` records except `paths_from_diff`, which parses `diff --git a/… b/…` headers because the PR rail has only a patch (`gh pr diff`), never a list. `core.quotepath=false` removes the escaping; a filename containing a space is still ambiguous in that header. | **open** — closes when the PR rail carries a name list beside the patch |
| L6 | The config writer is hand-rolled | 4 | The reader is `tomllib`. The writer stays ours because it preserves comments and unknown keys, which no TOML writer in the stdlib does. `test_written_values_read_back_unchanged` fails when either side moves alone. | **guarded** |
| L7 | One commit carried a whole round of work | 5 | `2b7dde1` landed ~11k lines closing seven classes at once, because that work predates this round and was already green. Bisect over it is coarse. Every commit after it is one class with one test. | **open as a class** — the rule is one defect, one commit, one test (`DEV_METHODOLOGY_GUIDELINES.md` §8) |

## Not classes, just work

- The debugger/repair rail is opt-in on apply (`--repair`) and is headed for
  its own rail. Do not make it automatic on apply again.
