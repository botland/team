# team

Deterministic orchestrator for a split software-delivery pipeline.

Agents emit **files**. This program owns **transitions**, git fences, and the test baseline. Runtimes are interchangeable adapters for each role — not workflow engines. Claude and Grok are the shipped pair; another headless terminal coding agent is a new adapter, not a new pipeline.

Nothing in this repository writes to `~/vibe.rc`, `~/.team`, or `~/.grok`. Put `~/team/bin` on your `PATH` yourself if you want `team` as a command.

```text
export PATH="$HOME/team/bin:$PATH"
```

## Default split

Roles are data. Change them per machine (`config.toml`), per repo (`.team/config.toml`), or per invocation (`--assign`). Per-role `--effort` is a separate table (`[effort]`, `--effort ROLE=LEVEL`) — not a new runtime name.

| Role | Default runtime | Writes |
|------|-----------------|--------|
| architect | claude | `design.md` only (via orchestrator) |
| critic | claude | `critic.md` (tries to kill the design) |
| tdd-design | claude | `test-contract.md` (no test files) |
| test-writer | grok | tests under `test_root` |
| implementer | grok | production under `code_root` (`.` = repo except `test_root` and git submodules) |
| tester | host | orchestrator runs the suite |
| adversarial | grok | attack tests under `test_root` + `adversarial.md` |
| debugger | claude | `diagnosis.md` (on failure) |
| reviewer | **both** | `review-claude.md`, `review-grok.md`, merged `review.md` |
| guardian | claude | `guardian.md` (R→A→T→I and I→R) |
| scout | grok | `scout.md` / `scout.json` (status-audit only) |

That ranking is a default, not a lock. Any coding role can run on any shipped adapter (or a registered third CLI). Swap freely:

```text
team --assign architect=grok --assign implementer=claude feature Add X
```

TDD is two roles on purpose. **tdd-design** answers “what should be true?”. **test-writer** turns that contract into files. **adversarial** asks “how can I make it fail?”. Those are different jobs.

## Commands

```text
team feature Add OAuth login with Google
team feature --dry-run Add OAuth login with Google
team feature --stop-after tdd-design Add OAuth login with Google
team resume oauth-login
team resume oauth-login --from implementer
team list
team review oauth-login
team apply oauth-login
team review
team review --reviewer claude
team review --reviewer both
team review --pr 12
team review --since reviewed-20260801-1200
team review --stamp
team review --list-tags
team review --show-range
team review --mark
team review --mark HEAD~3
team review --delete-tag reviewed-20260801-1200
team apply
team apply --seq
team review review-since-tag --seq
team replan oauth-login
team status oauth-login
team costs
team costs oauth-login
team audit
team audit ~/llm what's missing
team audit --depth thorough what's missing
team replan add-x --continue
team roles
team init
team config
team config --code-root . --test-root inferedge-phase1/tests
```

Global flags go **before** the subcommand:

```text
team --repo ~/ownedge/appliance-support --fake --test-command true feature Add greet helper
team --assign reviewer=claude --skip critic,adversarial feature Add X
team --assign all=grok resume review-since-tag
team --assign all=claude --assign implementer=grok apply
team --effort architect=4 --effort all=3 feature Add X
```

- `--dry-run` — architect + critic + TDD design; no test or production writes.
- `--fake` — no Claude/Grok calls; writes canned artifacts (for smoke tests).
- `--force` — replace an existing `.team/work/<slug>/`.
- `--skip critic,guardian,adversarial,debugger` — drop optional phases.
- `--effort ROLE=LEVEL` — per-role reasoning effort as a runtime-neutral `0`–`5`, `0` lowest (or `all=4`). Does not change `claude`/`grok` assignment.

Effort is an integer so it means the same thing to every runtime. Each CLI is sent the nearest rung it actually implements, so a level one of them lacks degrades rather than being dropped:

| level | claude | grok |
|---|---|---|
| 0 | low | low |
| 1 | low | low |
| 2 | medium | medium |
| 3 | high | high |
| 4 | xhigh | xhigh |
| 5 | **max** | xhigh — no fifth rung |

The names `low|medium|high|xhigh|max` are still accepted and stored as their level, so `xhigh` and `4` are one setting. `team roles` prints the mapping for the levels in use.

`team audit` is the status-audit pipeline (Grok `/audit`): scout → architect assess → dual review. It writes no production or test files. The human artifact is `report.md` (status + review). First leftover token is the repo if it is an existing directory, same as the Grok skill. Unlike `feature`, audit is allowed on a non-git tree.

Every command accepts `-h` / `--help` (`team review --help`, `team apply --help`, …). Global flags (`--repo`, `--assign`, `--effort`, `--fake`) go **before** the command.

`team review` without a slug is the vibe.rc `aireview` idea: **not only PRs**. Default scope is every commit since the last dedicated `reviewed-*` tag (`gittag` in vibe.rc). If none, the last git tag; if none, the whole branch. `--pr N` reviews a PR (`gh pr diff`, else merge-base with main/master) with **both** Claude and Grok. Past-commits review defaults to `review.range_reviewer` (grok). `claude`, `grok`, and `both` are valid in every mode; `both` runs Claude and Grok in parallel. Override with `team review --reviewer both` (or `team --assign reviewer=both review`).

`--stamp` (default on for `--pr`) writes `reviewed-YYYYMMDD-HHMM` at HEAD so the next unscoped review starts there. Manage the watermark without reviewing: `--list-tags`, `--show-range`, `--mark [ref]`, `--delete-tag TAG`, `--since REF`.

A new feature is three jobs:

```text
team feature Add OAuth login with Google   # implement (no review)
team list                                  # slug is oauth-login
team review oauth-login                    # reviewer + guardian; writes followups.md
team apply oauth-login                     # close classified findings
```

`team feature` stops after adversarial. Reviewer and guardian run only on `team review`. Apply does not review either.

`team apply` (or `team apply <slug>`) processes a review. Omit the slug to use `review-since-tag`, the same default as unscoped `team review`. Each finding needs `kind` (`architecture` | `implementation` | `test` | `note`). Apply does not invoke reviewer or guardian — loop with `team review && team apply` (range) or `team review <slug> && team apply <slug>` (feature). If the review is unstructured, apply stops (`needs-classification`). Then: architecture → design delta (replan writes `design.md` if it is missing); test → contract + tests (tdd-design writes the contract if it is missing); implementation → production; host suite. Test-writer may consult the implementer for the production shape; it must not edit `code_root`. Audit slugs are read-only and cannot be applied.

Debug and repair are **opt-in on apply**: `team apply --repair`. Without it a failing suite stops the run at `needs-repair` (or, with `--seq`, fails that class) and the worktree is left as the hops left it — the diagnose → repair → verify loop is the most expensive stretch of a hop and is moving to its own rail. `--repair` is still subject to `--skip debugger`.

`team apply <slug> --seq` closes **one class at a time** in the same order as `feature` (architecture → test → implementation, severity inside each kind) and loops until the queue is empty or a class fails. Host `UNVERIFIED` (no command / collection death / timeout) is not a class failure; a product `FAIL` is. Each hop sees only that class (not `review.md` / the rest of the backlog). Apply does not write a class review. Re-review a finished class with `team review <slug> --seq`; that writes `seq/<id>/review.md` and does not overwrite `review.md`. Each class writes `seq/<id>/checkpoint.json` and `delta.patch`. On failure the worktree is left as that class left it (no rollback). Retry the same class with `team apply <slug> --seq`; skip it with `--skip-failed`. If a later class shows an earlier decision was wrong, `team apply <slug> --seq --reopen <id>` opens that class again and marks later ids **stale** (not skipped); the next `--seq` retries it. Stale is temporary: when that reopened class is next **applied** or **skipped**, the suffix returns to the queue. Same-path guardian rows stay their own classes (context only; they do not inherit `applied`). `team list` prints each class id and status.

## Artifacts (the protocol)

Each run lives in the **target** repo, not in this engine:

```text
<repo>/.team/work/<slug>/
  brief.md
  design.md
  critic.md
  test-contract.md
  tdd-summary.md
  impl-summary.md
  baseline-report.md
  test-report.md
  review-claude.md
  review-grok.md
  review.md
  guardian.md
  adversarial.md
  adversarial-test-report.md
  diagnosis.md            # only if tests failed
  repair-summary.md       # after debugger repair
  verify-test-report.md
  followups.md            # open classes from review + guardian
  findings.json           # classified findings (after apply)
  apply-plan.md           # apply routing by kind
  apply-summary.md        # hops + suite after apply
  apply-seq.md            # apply --seq log (one class per step)
  seq/<id>/review.md      # team review --seq; does not replace review.md
  seq/<id>/checkpoint.json
  seq/<id>/delta.patch
  seq/<id>/reopen.md
  design-replan.md        # only after team replan / apply architecture
  census.md               # shared tree inventory (first inspect hop)
  usage.jsonl             # this-run hops (wiped by --force)
  usage.md                # human spend table
  scout.md / scout.json   # audit only
  status.md               # audit only
  report.md               # audit: status + review (primary)
  state.json
  consult/001-implementer-architect.json
  git/after-test-writer.txt
  prompts/architect.prompt.md
```

After each model hop the orchestrator copies token counts and `$` from the headless JSON envelope into the slug `usage.jsonl` and the durable `.team/work/usage.jsonl` (survives `team review --force`). Console:

```text
usage  reviewer-grok (grok)  7.2k in / 1.9k out / 41k cache  $0.0127  22 turns
usage  2 hops  4.01M tokens  $0.49
```

`$` is only written when the CLI stamped a complete cost. Omitted `$` is unknown, never free. A failed apply still prints the running total. `--fake` logs the hop without inventing `$`. `team costs` reads the durable ledger. Console spend uses the findings palette (green complete `$`, yellow unknown, cyan tokens). `NO_COLOR` / `FORCE_COLOR` apply. `usage.md` stays uncolored.

`team init` writes `<repo>/.team/config.toml` and ignores `work/`. `team config` shows the effective project config, or writes individual keys into that file (`--code-root`, `--test-root`, `--test-command`, `--assign`, `--effort`, `--skip`, `--range-reviewer`, `--phase-timeout`, or `KEY=VALUE`). It does not write the engine `config.toml`.

## What the orchestrator enforces

1. **The write fence is authoritative.** After test-writer, new in-repo dirty paths must sit under `test_root` (plus `.team/work/`) and not under a distinct `code_root` or a foreign git submodule. After implementer, under `code_root` and not under `test_root` or a foreign git submodule. `code_root='.'` is the repository with those exclusions. Unset `test_root` still excludes the conventional `tests/` tree (testhost's discovery fallback). `test_root='.'` is the same shape for tests: the repository except `code_root`. A git target is observed via porcelain (rename source included); a non-git target is walked. Extra-worktree leaves named by product law (`vibe.rc`, `.team` at `--repo`'s parent and `$HOME`) fail the hop and are restored. A directory leaf is walked in full. Agent `paths_touched` is ignored. A hop may edit files that were already dirty; the fence is the role root, not a clean tree. CLI `--deny` is an approximation.
2. **Baseline vs final.** The host runs `test_command` (or a discovered command) before and after implement. `test_root` is the write fence, not that command. Verdicts: `PASS`, `FAIL`, `UNVERIFIED`, `REGRESSION`, `BROKEN_BASELINE`. Collection death (suite never ran) and a host timeout are `UNVERIFIED`, not a product `FAIL`.
3. **Consults are files.** A writer that is not ready returns questions. The orchestrator calls the named role and then invokes the writer again with the answers in the prompt. Each model hop is a new session. Consults read `census.md` and listed artifacts; they do not recensus the repo. Cross-vendor (Grok implementer → Claude architect) is the same path.
4. **Dual review.** When `reviewer=both`, Claude and Grok review independently and never see each other’s report. Merge is deterministic (concat + overlap on path+title).
5. **Replan is a delta.** `design-replan.md` uses unchanged / changed / new / removed criteria and structural changes. `team replan --continue` merges that delta into the living `design.md` and resumes from TDD design. `team apply` also replans when findings are `kind=architecture`, then routes test and implementation findings without replaying the whole feature rail.
6. **Apply needs classified findings.** Review schema requires `kind` on every finding. Apply stops if kind is missing (`team review` classifies). Then one hop per owner. Apply does not run reviewer or guardian.
7. **Failed tests get one repair hop.** Debugger names an owner (`implementer` or `test-writer`); that role patches once; the host re-runs the suite. On the `feature` rail this is automatic; on apply it is opt-in (`team apply --repair`).
8. **Adversarial writes tests.** Attack vectors become files under `test_root`, then the host runs the suite again.
9. **Audit is read-only.** After scout / assess / review, new dirty paths must sit under `.team/work/` only — including on a tree with no `.git`. Extra-worktree `vibe.rc` / `.team` writes fail the hop. Claims of done/WIP/missing need path-level evidence; the reviewer treats the scout inventory as untrusted. Review findings on an audit slug cannot be applied.

## Config

Copy `config.example.toml` to `config.toml` in this repo (gitignored) for machine defaults, or to `<target>/.team/config.toml` for a project. Persist a project's roots and other file-backed options with `team config` (creates `.team/config.toml` from the example if needed):

```text
team --repo ~/ownedge config --code-root . --test-root inferedge-phase1/tests
team config --assign reviewer=claude --skip critic
team config --effort architect=4
team config test_command="make test"
team config --unset code_root
```

`test_root` is where test-writer may edit. `test_command` is what the host runs. A nested test tree that needs a working directory or `PYTHONPATH` must set `test_command`; discovery will not invent that.

```toml
[roles]
architect = "claude"
tdd-design = "claude"
test-writer = "grok"
implementer = "grok"
reviewer = "both"
```

```toml
[review]
range_reviewer = "grok"
```

```toml
[effort]
architect = 4
reviewer = 4
```

## Layout of this repo

```text
bin/team           # launcher (sets TEAM_HOME)
src/team/          # orchestrator
personas/          # one file per role — the only copy of the role text
schemas/           # JSON schemas for model outputs
tests/             # unittest (no network, --fake pipeline)
```

Personas are the single source of truth. Do not fork them into `.claude/agents` or `.grok/personas` unless those files only point here.

## Tests

```text
make test
# or
python3 -m unittest discover -s tests -v
```

Requires `git` and Python 3.9+. No third-party packages.

## Requirements on the machine

- `claude` on `PATH` for Claude-assigned roles
- `grok` on `PATH` for Grok-assigned roles
- Target path must be a git repo (`~/tmp` is not one)

Override binaries with `TEAM_CLAUDE` / `TEAM_GROK`.

## Headless, always

`bin/team` never opens a TUI. The two CLIs disable interactive chrome differently:

| Runtime | How this repo turns the TUI off |
|---------|----------------------------------|
| Claude | `-p` / `--print` plus `--output-format json` |
| Grok | `--prompt-file` (headless trigger) plus `--no-alt-screen` (beats a fullscreen user config) |

Both children also get `CI=1`. Prompt text is written under `.team/work/<slug>/prompts/` so you can inspect what ran without sitting in either TUI.

## Shared engineering rules

Every agent prompt includes [`docs/engineering.md`](docs/engineering.md): enumerate the space, close the class, boundary vs approximation, seams, vacuous guards, spec as input. That file is repo-agnostic. Product law stays in the target's own `AGENTS.md`.
