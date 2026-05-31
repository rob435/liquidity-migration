# liquidity-migration

A research codebase for a Bybit liquidity-migration short strategy. The
strategy is under **active research** — its signal is statistically real but
regime-conditional. All research findings are consolidated in
`docs/research_summary.md` (see `STATE.md` for live/operational state). Python
3.11+; package `liquidity_migration/`.

@AGENTS.md

## Orientation — read FIRST

- **`STATE.md`** (repo root) — single-page research-program state. What's
  done, what's running, what's next. **First read for every session.**
- **`docs/backtesting_errors_we_never_repeat.md`** — mandatory research
  methodology standard. Read it before any backtest or strategy work.
- **`docs/research_summary.md`** — the single consolidated research record:
  all results, verdicts, the useful findings, and the three-tier **demo-arbiter**
  decision framework (Investigation → Demo-candidate → Real-money), MAR-primary.
  (Round 1 + Round 2 per-phase docs were consolidated here; originals in git history.)
- `docs/research_findings.md` — short verdict pointer (defers to the summary).
- `docs/data_roots.md` — which data root to use (research vs. live demo vs. OOS).
- `docs/event_demo_daemon.md` — demo daemon runbook + live-infra hardening changelog.
- `README.md` — repo overview and status.

## Running

- CLI: `python -m liquidity_migration <subcommand>` (run `--help` for the list).
- Tests: `.venv/bin/python -m pytest -q`.

## Mandatory pre-push gate

**Before EVERY `git push` on this repo, run BOTH commands the local pre-push
hook (`.git/hooks/pre-push`) enforces:**

```bash
.venv/bin/python -m ruff check liquidity_migration tests
.venv/bin/python -m pytest -q
```

If `ruff` fails, fix with `ruff check --fix` and re-verify. If pytest fails,
fix the tests before pushing. The hook blocks the push on failure; and because a
push to `main` auto-deploys to the live VPS (`.github/workflows/vps-deploy.yml`,
which emails the operator on failure), pushing broken code is operator pain. No
exceptions.

## Working here

- Project skills in `.claude/skills/` auto-load by description when relevant
  (methodology gate, CLI invocations, reconcile, equity curve, research workflow,
  report interpretation, repo navigation). `ls .claude/skills/` for the current set.
- The `liqmig-research` MCP server exposes:
  - `current_state` — STATE.md, in 60 seconds
  - `data_roots` — canonical data-root index
  - `list_reports`, `parse_report`, `audit_run_artifacts` — report tooling
  - `apply_decision_rule(summary_csv)` — programmatic verdict (legacy strict Sharpe bar; the Tier-2 demo-candidate verdict comes from `scripts/r1_robustness.py`; tier definitions in STATE.md)
- Research + reconcile helper scripts (`volume_events_cell.sh`, `r1_robustness.py`,
  `apply_decision_rule.py`, `reconcile.sh`, …) are driven by the `research-phase-runner`
  and `pit-reconcile` skills; STATE.md "Helpers" is the canonical roster. Use those
  rather than hand-assembling the calls.
- For architecture questions, read `graphify-out/GRAPH_REPORT.md` first.
