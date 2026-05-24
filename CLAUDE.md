# liquidity-migration

A research codebase for a Bybit liquidity-migration short strategy. The
strategy is under **active research** — its signal is statistically real but
regime-conditional, and the current focus is regime-aware refinement (see
`README.md` and `docs/research_findings.md`). Python 3.11+; package
`liquidity_migration/`.

@AGENTS.md

## Orientation

- **`docs/backtesting_errors_we_never_repeat.md`** — mandatory research
  methodology standard. Read it before any backtest or strategy work.
- `docs/research_findings.md` — current research verdict and refinement roadmap.
- `docs/data_roots.md` — which data root to use (research vs. live demo vs. OOS).
- `docs/system_status.md` — strategy / deployment status.
- `README.md` — repo overview and status.

## Running

- CLI: `python -m liquidity_migration <subcommand>` (17 subcommands; see `--help`).
- Tests: `.venv/bin/python -m pytest -q`.

## Mandatory pre-push gate (CI parity)

**Before EVERY `git push` on this repo, run BOTH commands the CI workflow
(`.github/workflows/ci.yml`) runs:**

```bash
.venv/bin/python -m ruff check liquidity_migration tests
.venv/bin/python -m pytest -q
```

If `ruff` fails, fix with `ruff check --fix` and re-verify. If pytest fails,
fix the tests before pushing. The user gets a GitHub email on every CI
failure — pushing broken code is operator pain. No exceptions.

## Working here

- Project skills in `.claude/skills/` load automatically when relevant:
  `backtest-integrity` (methodology gate), `run-strategy` (CLI invocations),
  `research-report` (report interpretation), `repo-map` (codebase navigation).
- The `liqmig-research` MCP server exposes data-root and report-parsing tools.
- For architecture questions, read `graphify-out/GRAPH_REPORT.md` first.
