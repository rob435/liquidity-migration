---
name: research-phase-runner
description: "Execution workflow for the SELECTION-vs-EXECUTION research plan at docs/research_plan_selection_execution.md. Use any time you are about to run, conditionally-run, or write up an experiment (E1 execution-premium, E2 continuous+execution, E3 sniper) from that plan — covers pre-checks, dispatch, the three-tier demo-arbiter decision rule (scripts/r1_robustness.py + scripts/apply_decision_rule.py), the verdict receipt, STATE.md update, and the commit. Keeps the three-tier thresholds intact; the Tier-3 real-money gate stays strict."
---

# Running a research experiment

Use this every time you run, conditionally-run, or write up an experiment from the
plan at **`docs/research_plan_selection_execution.md`** (E1 execution-premium,
E2 continuous + execution, E3 sniper).

Always read **STATE.md** first — it tells you what's done, what's pending, and the
current binding decision rules.

## The thesis you are testing

The strategy is two separable signals: **selection** (the liquidity-migration event
picks a candidate pool) and **execution** (short the *confirmed fade* — pop then
giveback — not the top). It is a **fade strategy, not a catch-the-top strategy**. The
plan tests the two signals separately; the old "null" measured one worst-case
execution and blamed the signal. Do not re-introduce that conflation.

## The decision framework — three-tier, demo-arbiter

Ordered by how expensive a false positive is:

1. **Investigation** — keep studying? Loose: MAR Δ > 0 on the majority of venues, no
   return sign-flip, trade minimums.
2. **Demo-candidate** (→ forward demo) — LOOSE: **return positive on both venues +
   pooled MAR Δ > +0.1 + neither venue worse than −0.5 MAR + ≥30 by / ≥20 bn trades.**
   Fragility diagnostics (bootstrap p5, leave-one-month-out, sub-period thirds,
   residual Sharpe) are **reported, NOT blocking** — they set demo order.
3. **Real-money** (demo → mainnet) — STRICT, NOT loosened: forward-demo OOS pass
   (≥30d forward demo + daily paper reconciliation) + bootstrap pooled MAR-Δ p5 ≥ 0
   + residual Sharpe ≥ +0.3 + stress pass + capacity. There is no internal pre-2023
   OOS root — pristine OOS is the forward demo/paper ledgers (`docs/data_roots.md`).

Principle: permissive where being wrong is free (backtest→demo is paper), strict where
it costs real money. The forward demo is both the multiple-testing arbiter and the OOS
surface — uncapped. MAR-primary (pooled), Sharpe secondary.

## Workflow (apply per experiment)

1. **Pre-check.** Read STATE.md. Confirm the experiment's gate is met (E2 needs E1's
   execution premium; E3 needs E1/E2's timing result). Confirm required code is
   merged. Confirm data roots present (`~/SHARED_DATA/{bybit,binance}_full_pit`).

2. **Plan the arms/cells.** Re-read the experiment's section in the plan. Do NOT add
   off-menu cells without a dated amendment (a new pre-registration receipt) first.

3. **Dispatch.** Single cells: `scripts/volume_events_cell.sh` (fills the
   production-baseline flags; you pass overrides). Multi-cell sweeps: write a
   `scripts/_sweep_runtime.py`-based dispatcher for the experiment (the old R-phase
   dispatchers were deleted — `_sweep_runtime.py` is the reusable primitive: a
   dispatcher declares `BASELINE_PARAMS` + a list of `Cell`s and imports it).
   **Full-PIT sweeps run at `SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8`** — one
   full-PIT cell peaks ~23 GB, so 8 workers OOMs the box. Only light (non-full-PIT)
   sweeps use `SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4`. Always run **both venues**.

4. **Apply the decision rule.**
   ```bash
   python scripts/r1_robustness.py --sweep-tag <SWEEP_TAG>
   ```
   emits the pooled-MAR-Δ Tier-2 verdict (engine-DD MAR) + bootstrap p5,
   leave-one-month-out, and sub-period thirds from the per-cell ledgers.
   `scripts/apply_decision_rule.py <SUMMARY_CSV> --control <CONTROL_CELL>` is the
   **legacy strict (Sharpe) bar** — reference only; not the promotion gate.
   Do not move thresholds downward to rescue a cell (see non-negotiables).

5. **Write the verdict.** Dated receipt under `docs/preregistration/<YYYY-MM-DD>-<exp>-verdict.md`
   with: experiment, full per-arm/cell metrics, the Tier-2 verdict + fragility, the
   verdict paragraph (incl. the falsifier outcome — a negative result is first-class),
   the forward pointer, AND a one-paragraph roll-up into `docs/research_summary.md`.

6. **Update STATE.md.** Move the experiment to its terminal state; add new helpers /
   open questions. Keep STATE.md under ~120 lines.

7. **Commit + propose push to operator.** Pre-push gate
   (`.venv/bin/python -m ruff check liquidity_migration tests` +
   `.venv/bin/python -m pytest -q`) MUST pass. NEVER push without operator confirmation.

## Pre-committed behaviours (non-negotiable)

- **No FURTHER loosening.** The framework was loosened ONCE, on principle,
  pre-registered. Do not loosen again to rescue a near-miss; the Tier-3 real-money
  gate stays strict. A cell short of the Tier-2 bar is descriptive, not a candidate.
- **No off-menu cells.** New cells need a dated pre-registration amendment first.
- **Gates are real.** A failed gate (E1 says selection-only; E2 doesn't clear Tier-2;
  E3 doesn't beat the 1h squeeze) means file a one-paragraph negative-trigger note and
  stop — don't look for excuses to run the gated phase. **Failure is a first-class
  outcome**; the strategy stays frozen. There is no "ship something" obligation.
- **Both venues.** Cross-venue agreement is the robustness bar; a single-venue edge
  does not clear Tier-2.

## Useful MCP tools

- `current_state()` — STATE.md as structured data.
- `apply_decision_rule(summary_csv, control_cell)` — programmatic legacy-bar verdict
  (reference only; the Tier-2 verdict is `r1_robustness.py`).
- `parse_report(path)` — `volume_event_research_report.md` → headline metrics.
- `audit_run_artifacts(path)` — integrity-standard artifact completeness.
- `data_roots()` — current data-root index.

## Communication style during an experiment

Report after each experiment ends with: a 2-line headline (what ran, what verdict);
the Tier-2 verdict + fragility output (~10 lines); the verdict file path; the next
experiment to trigger (or "gate failed — stop"). Do NOT report mid-run progress unless
something fails — let sweeps run to completion.

Operator is learning quant fundamentals — explain in plain language, and surface
inconsistencies BEFORE running.
