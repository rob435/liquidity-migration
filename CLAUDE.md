# liquidity-migration

A research codebase for a Bybit liquidity-migration short strategy. The
strategy is under **active research** — its signal is statistically real but
regime-conditional. Current focus: the **Round 2 integrated-strategy program**
(see `STATE.md` and `docs/preregistration/round2/`). Python 3.11+; package
`liquidity_migration/`.

@AGENTS.md

## Orientation — read FIRST

- **`STATE.md`** (repo root) — single-page research-program state. What's
  done, what's running, what's next. **First read for every session.**
- **`docs/backtesting_errors_we_never_repeat.md`** — mandatory research
  methodology standard. Read it before any backtest or strategy work.
- **`docs/preregistration/round2/integrated-strategy-program.md`** — the
  CURRENT research plan (Round 2). Three-tier **demo-arbiter** decision
  framework (Investigation → Demo-candidate → Real-money), MAR-primary.
- `docs/preregistration/round1/` — Round 1 plan + verdicts (COMPLETE,
  documented null). Historical evidence, not the active plan.
- `docs/research_findings.md` — older research verdict + refinement roadmap.
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
  `research-phase-runner` (multi-phase workflow), `research-report` (report
  interpretation), `repo-map` (codebase navigation).
- The `liqmig-research` MCP server exposes:
  - `current_state` — STATE.md, in 60 seconds
  - `data_roots` — canonical data-root index
  - `list_reports`, `parse_report`, `audit_run_artifacts` — report tooling
  - `apply_decision_rule(summary_csv)` — programmatic verdict (legacy strict bar; the Round-2 Tier-2 demo-candidate verdict comes from `scripts/r1_robustness.py`)
- Research-phase helpers (no skills needed):
  - `scripts/volume_events_cell.sh --venue X --cell-id Y --phase Z --overrides 'K=V,…'`
    — runs `volume-events` with production-baseline flags filled in.
  - `scripts/apply_decision_rule.py SUMMARY.csv` — CLI form of the
    decision-rule analyzer; produces a per-cell verdict table.
- For architecture questions, read `graphify-out/GRAPH_REPORT.md` first.
