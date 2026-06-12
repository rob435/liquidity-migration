# liquidity-migration

A research codebase for Bybit crypto-perp trading research (continuous fade
book + long-only v11a sleeve). **The original daily SHORT strategy was ERASED
2026-06-11 by operator order** — git history is the archive. All research
findings are consolidated in `docs/research_summary.md` (see `STATE.md` for
live/operational state). Python 3.11+; package `liquidity_migration/`.

@AGENTS.md

## Orientation — read FIRST

- **`STATE.md`** (repo root) — single-page research-program state. What's
  done, what's running, what's next. **First read for every session.**
- **`docs/backtesting_errors_we_never_repeat.md`** — mandatory research
  methodology standard. Read it before any backtest or strategy work.
- **`docs/research_summary.md`** — THE single consolidated research record:
  every result, verdict, useful finding, open methodology debt. The three-tier
  **demo-arbiter** decision framework is defined in STATE.md "Decision Rules (three-tier demo-arbiter)" (MAR-primary). All per-arc write-ups + research scripts were consolidated
  here and removed 2026-06-02; git history is the backstop. **Read this for all
  research; it is the one file.**
- `docs/data_roots.md` — which data root to use (research vs. live demo vs. OOS).
- `README.md` — repo overview and status.

## Running

- CLI: `python -m liquidity_migration <subcommand>` (run `--help` for the list).
- Tests: `.venv/bin/python -m pytest -q`.

## Mandatory pre-push gate

**Before EVERY `git push` on this repo, run BOTH commands the local pre-push
hook (`.git/hooks/pre-push`) enforces:**

```bash
.venv/bin/python -m ruff check liquidity_migration tests scripts
.venv/bin/python -m pytest -q
```

If `ruff` fails, fix with `ruff check --fix` and re-verify. If pytest fails,
fix the tests before pushing. The hook blocks the push on failure; and because a
push to `main` auto-deploys to the live VPS (`.github/workflows/vps-deploy.yml`,
which emails the operator on failure), pushing broken code is operator pain. No
exceptions.

The hook's canonical tracked source is `scripts/git-hooks/pre-push`
(`.git/hooks/` is untracked, so a fresh clone has NO gate). Install on any new
clone/machine:

```bash
cp scripts/git-hooks/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push
```

## Working here

- Project skills in `.claude/skills/` auto-load by description when relevant
  (methodology gate, CLI invocations, reconcile, equity curve, research workflow,
  report interpretation, repo navigation, VPS migration/rebuild).
  `ls .claude/skills/` for the current set.
- The `liqmig-research` MCP server exposes:
  - `current_state` — STATE.md, in 60 seconds
  - `data_roots` — canonical data-root index
  - `list_reports`, `parse_report`, `audit_run_artifacts` — report tooling
  - `apply_decision_rule(summary_csv)` — programmatic verdict (legacy strict Sharpe bar; the Tier-2 demo-candidate verdict comes from `scripts/r1_robustness.py`; tier definitions in STATE.md)
- Research + reconcile helper scripts (`r1_robustness.py`, `apply_decision_rule.py`,
  `reconcile.sh`, …) are driven by the `research-phase-runner` and `pit-reconcile`
  skills; STATE.md "Helpers" is the canonical roster. Use those rather than
  hand-assembling the calls.
- For architecture questions, read `graphify-out/GRAPH_REPORT.md` first.
