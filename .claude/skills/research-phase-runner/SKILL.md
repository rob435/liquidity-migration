---
name: research-phase-runner
description: "Execution workflow for the Round 2 research program pre-registered at docs/preregistration/round2/integrated-strategy-program.md. Use any time you are about to run, conditionally-run, or write up a sub-phase (R0-R12, C0-C3) from that plan — covers pre-checks, dispatch, the three-tier demo-arbiter decision rule (scripts/r1_robustness.py + scripts/apply_decision_rule.py), STATE.md update, and per-phase verdict commit. Keeps the three-tier thresholds intact; the Tier-3 real-money gate stays strict."
---

# Running a research sub-phase (Round 2)

Use this every time you run, conditionally-run, or write up a sub-phase of the
Round 2 program. The plan lives at
`docs/preregistration/round2/integrated-strategy-program.md`.

Always read STATE.md first — it tells you what's done, what's pending, and the
current binding decision rules.

## The decision framework — three-tier, demo-arbiter

Round 2 replaced Round 1's single strict "Strictness Manifesto" bar (+ FDR
ceiling) with three gates, ordered by how expensive a false positive is:

1. **Investigation** (R1-R8 sub-phases) — keep studying? Loose: MAR Δ > 0 on
   the majority of venues, no return sign-flip, trade minimums.
2. **Demo-candidate** (R10 gate → forward demo + queue for R11 OOS) — onto the
   free forward-demo treadmill? LOOSE: **return positive on both venues +
   pooled MAR Δ > +0.1 + neither venue worse than -0.5 MAR + ≥30 by / ≥20 bn
   trades.** Fragility diagnostics (bootstrap p5, leave-one-month-out,
   sub-period thirds, residual Sharpe) are **reported, NOT blocking** — they
   set demo order.
3. **Real-money** (demo → mainnet) — STRICT, NOT loosened: pre-2023 OOS pass +
   ≥30d forward demo + bootstrap pooled MAR-Δ p5 ≥ 0 + residual Sharpe ≥ +0.3
   + R7 stress pass + R8 capacity.

Principle: permissive where being wrong is free (backtest→demo costs nothing —
demo is paper), strict where it costs real money. The forward demo is the
multiple-testing arbiter; the only finite surface capped is the pre-2023 OOS
root (5 cells/quarter). MAR-primary (pooled), Sharpe secondary.

## Phase-runner workflow (apply per sub-phase)

1. **Pre-check.** Read STATE.md. Confirm upstream sub-phases completed and
   produced the inputs this one needs. Confirm required code changes are
   merged. Confirm data roots present (`~/SHARED_DATA/{bybit,binance}_full_pit`).

2. **Plan the cells.** Re-read the sub-phase's "Cell list" table in the plan.
   Do NOT add off-menu cells without a dated amendment to the plan first.

3. **Dispatch.** Single cells: `scripts/volume_events_cell.sh` (fills the
   production-baseline flags; you pass overrides). Multi-cell sweeps: a
   `scripts/_sweep_runtime.py`-based orchestrator (e.g.
   `scripts/r1_filter_audit_sweep.py`). **Full-PIT sweeps run at
   `SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8`** — one full-PIT cell peaks ~23 GB, so
   8 workers OOMs the box (`_sweep_runtime.py` is memory-aware and auto-caps, but set
   it explicitly). Only light (non-full-PIT) sweeps use `SWEEP_MAX_WORKERS=8
   POLARS_MAX_THREADS=4`.

4. **Apply the decision rule.**
   - **Tier-2 demo-candidate verdict + fragility** (the Round-2 default):
     ```bash
     python scripts/r1_robustness.py --sweep-tag <SWEEP_TAG>
     ```
     emits the pooled-MAR-Δ Tier-2 verdict (engine-DD MAR) + bootstrap p5,
     leave-one-month-out, and sub-period thirds from the per-cell ledgers.
   - `scripts/apply_decision_rule.py <SUMMARY_CSV> --control 00_baseline` is the
     **legacy strict (Sharpe) bar** — reference only; do not use it as the
     Round-2 promotion gate.
   Do not move thresholds downward to rescue a cell (see non-negotiables).

5. **Write the verdict.** Create the dated verdict file under
   `docs/preregistration/round2/<YYYY-MM-DD>-<phase>-verdict.md` with: stage,
   full per-cell metrics, the Tier-2 verdict + fragility diagnostics, the
   verdict paragraph (incl. any falsification), and the forward pointer to the
   next sub-phase (or "program complete — documented null").

6. **Update STATE.md.** Move the sub-phase to its terminal state in the table;
   add new helpers / open questions.

7. **Commit + propose push to operator.** Two-file commit (verdict + STATE.md).
   Pre-push gate (`ruff check liquidity_migration tests` + `pytest -q`) MUST
   pass. NEVER push without operator confirmation.

## Sub-phase scope + sequencing (Round 2)

- **Data-only sweeps run now:** R1 (per-filter audit, **wide funnel
  `max_active=12`**), R2 (per-feature decile-sort), R3 (bearish stack). These
  use `cli.py → volume_events.py`, unaffected by the in-flight `event_demo` /
  `volume_events` refactor (CLI verified intact).
- **Code-touch phases build on the post-refactor module layout** (event_demo
  split into `event_demo_{data,entries,planning,exits,reports,daemon}.py`):
  R4 (risk model), R5 (sizing), R6 (cost model), R12 (sniper), C0 (continuous
  engine / daemon). Verify the exact module per hook; coordinate if more
  refactor work is in flight.
- **Conditional triggers** are defined per sub-phase in the plan (e.g. R3's
  market-neutral leg, R12d/f on a sniper flavor beating market@1h, C3 on a C2
  demo-candidate). If a trigger isn't met, the phase does NOT run — file a
  1-paragraph negative-trigger note; don't look for excuses to run it.
- **Lead candidate `R1_drop_all_4`** cleared Tier-2 in the ORIGINAL R1 verdict but
  **FALSIFIES Tier-2 under the `9f52819` hardened re-baseline** (bar_extreme stops +
  100% taker + calendar returns; pooled MAR Δ +0.45→+0.05, binance negative). The
  re-baseline cascade premise is therefore falsified: the R9 baseline is
  `R9_event_only` (production), NOT drop_all_4. Always confirm the current baseline
  from STATE.md before dispatching. See
  [r1-rebaseline-hardened-verdict.md](../../../docs/preregistration/round2/r1-rebaseline-hardened-verdict.md).

## Pre-committed behaviours (non-negotiable)

- **No FURTHER loosening.** The framework was loosened ONCE this round, on
  principle (venue heterogeneity + redundant tests), pre-registered and
  re-applied blind. Do not loosen again to rescue a near-miss, and the Tier-3
  real-money gate stays strict. A cell short of the Tier-2 bar is descriptive,
  not a demo-candidate.
- **No off-menu cells.** New cells require a dated amendment to the plan before
  running, with operator review.
- **R11 OOS is the final gate** before any real-money conversation; a Tier-3
  failure means closed. No escape-hatch phase.
- **No hard end-date.** "Weeks if needed" per operator instruction; the
  discipline is in not shortcutting the workflow, not racing a deadline.
- **Failure is a first-class outcome.** Zero finalists = documented null;
  strategy stays frozen. There is no "ship something" obligation.

## Phase-specific gotchas

- **R1 (filter audit, wide funnel):** runs at `max_active=12` (not the
  production 3) to gather a large dataset for feature-filtering. Dropping a
  filter = its permissive sentinel (canonical Phase-0 LOO values). The control
  also runs at 12, so numbers are NOT comparable to the max_active=3 peek.
- **Sweep dispatch:** every orchestrator inherits `scripts/_sweep_runtime.py`
  (parallel dispatch + per-cell summary flush). It emits a `window_days`
  column so analytics stay window-aware.
- **Signal-harness panels (R2 features):** the panel-build step is a one-off
  per venue — cache it; re-running IC / decile-sort on the cached panel is
  cheap. Do NOT rebuild the panel per run.
- **R11 OOS:** assess pre-2023 root state on first contact. If a rebuild is
  needed (~6h/venue download), flag to operator before kicking off.

## Useful MCP tools

- `current_state()` — STATE.md as structured data.
- `apply_decision_rule(summary_csv, control_cell)` — programmatic legacy-bar
  verdict (reference only; Round-2 Tier-2 verdict is `r1_robustness.py`).
- `parse_report(path)` — `volume_event_research_report.md` → headline metrics.
- `audit_run_artifacts(path)` — integrity-standard artifact completeness.
- `data_roots()` — current data-root index.

## Communication style during a phase

Report after each sub-phase ends with: a 2-line headline (what ran, what
verdict); the Tier-2 verdict + fragility output (~10 lines); the verdict file
path; the next sub-phase to trigger (or "program complete"). Do NOT report
mid-run progress unless something fails — let sweeps run to completion.

Operator is learning quant fundamentals — explain in plain language, and
surface inconsistencies BEFORE running.
