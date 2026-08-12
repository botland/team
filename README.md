# team

Deterministic orchestrator for a split software-delivery pipeline.

Agents emit **files**. This program owns **transitions**, git fences, and the test baseline. Claude and Grok are interchangeable runtimes for each role — not workflow engines.

Nothing in this repository writes to `~/vibe.rc`, `~/.team`, or `~/.grok`. Put `~/team/bin` on your `PATH` yourself if you want `team` as a command.

```text
export PATH="$HOME/team/bin:$PATH"
```

## Default split

Roles are data. Change them per machine (`config.toml`), per repo (`.team/config.toml`), or per invocation (`--assign`).

| Role | Default runtime | Writes |
|------|-----------------|--------|
| architect | claude | `design.md` only (via orchestrator) |
| critic | claude | `critic.md` |
| tdd-design | claude | `test-contract.md` (no test files) |
| test-writer | grok | tests under `test_root` |
| implementer | grok | production under `code_root` |
| tester | host | orchestrator runs the suite |
| adversarial | grok | `adversarial.md` (report only) |
| debugger | claude | `diagnosis.md` (on failure) |
| reviewer | **both** | `review-claude.md`, `review-grok.md`, merged `review.md` |
| guardian | claude | `guardian.md` |
| scout | grok | `scout.md` / `scout.json` (status-audit only) |

That matches a role-specialized ranking: high-leverage reasoning (architecture, TDD design, review, invariants) on Claude; execution and attack-generation (test files, implement, adversarial) on Grok. Swap freely:

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
team review oauth-login
team replan oauth-login
team status oauth-login
team audit
team audit ~/llm what's missing
team audit --depth thorough what's missing
team roles
team init
```

Global flags go **before** the subcommand:

```text
team --repo ~/ownedge/appliance-support --fake --test-command true feature Add greet helper
team --assign reviewer=claude --skip critic,adversarial feature Add X
```

- `--dry-run` — architect + critic + TDD design; no test or production writes.
- `--fake` — no Claude/Grok calls; writes canned artifacts (for smoke tests).
- `--force` — replace an existing `.team/work/<slug>/`.
- `--skip critic,guardian,adversarial,debugger` — drop optional phases.

`team audit` is the status-audit pipeline (Grok `/audit`): scout → architect assess → dual review. It writes no production or test files. The human artifact is `report.md` (status + review). First leftover token is the repo if it is an existing directory, same as the Grok skill. Unlike `feature`, audit is allowed on a non-git tree.

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
  diagnosis.md            # only if tests failed
  design-replan.md        # only after team replan
  scout.md / scout.json   # audit only
  status.md               # audit only
  report.md               # audit: status + review (primary)
  state.json
  consult/001-implementer-architect.json
  git/after-test-writer.txt
  prompts/architect.prompt.md
```

`team init` writes `<repo>/.team/config.toml` and ignores `work/`.

## What the orchestrator enforces

1. **Git is authoritative.** After test-writer, new dirty paths must sit under `test_root` (plus `.team/work/`). After implementer, under `code_root`. Agent `paths_touched` is ignored for the fence.
2. **Baseline vs final.** The host runs `test_command` (or a discovered command) before and after implement. Verdicts: `PASS`, `FAIL`, `UNVERIFIED`, `REGRESSION`, `BROKEN_BASELINE`.
3. **Consults are files.** A writer that is not ready returns questions. The orchestrator calls the named role and resumes the writer. Cross-vendor (Grok implementer → Claude architect) is the same path.
4. **Dual review.** When `reviewer=both`, Claude and Grok review independently and never see each other’s report. Merge is deterministic (concat + overlap on path+title).
5. **Replan is a delta.** `design-replan.md` uses unchanged / changed / new / removed criteria and structural changes. It does not start implement.
6. **Audit is read-only.** After scout / assess / review, new dirty paths must sit under `.team/work/` only. Claims of done/WIP/missing need path-level evidence; the reviewer treats the scout inventory as untrusted.

## Config

Copy `config.example.toml` to `config.toml` in this repo (gitignored) for machine defaults, or to `<target>/.team/config.toml` for a project.

```toml
[roles]
architect = "claude"
tdd-design = "claude"
test-writer = "grok"
implementer = "grok"
reviewer = "both"
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
