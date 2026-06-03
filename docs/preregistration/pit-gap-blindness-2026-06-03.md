# Pre-registration: PIT gap-blindness in the daily feature path (BAC-1 / BAC-5 / BAC-7)

**Date:** 2026-06-03
**Author:** audit (Opus 4.8)
**Stage:** run-pending

## What's changing
Make the daily feature computation **calendar-aware** instead of **positional**: a
per-symbol `shift(N)` / `tail(6)` over the daily grid currently uses the Nth *present*
row, which spans more than N calendar days (or more than 6 hours) when a symbol has a
mid-history data gap (delist/relist, archive hole). Three sites:

- **BAC-1** (`volume_events_features._daily_return_frame` + `_enriched_event_features`, plus
  the per-symbol daily rollings across the funding/flow/basis/premium/market frames and
  `volume_features`): positional `shift(N).over("symbol")` for `return_3d/7d/14d/30d`,
  `prior7_/prior14_return`, the `prior_/prior7_` rank-fraction shifts, and crucially
  **`liquidity_rank.shift(7)` → `prior7_liquidity_rank` → `liquidity_rank_improvement_7d`**,
  the FLAGSHIP `liquidity_migration_rank_improvement_min=150` gate — **and** the
  `rolling_max/min/std/mean/sum(window=N).over("symbol")` windows (an N-*row* window spans
  >N calendar days across a gap; see the rolling-residual entry under Implementation status).
- **BAC-5** (`_daily_return_frame`): `signal_day_last6h_open/turnover` use `tail(6)` ROWS,
  not the last 6 HOURS — a gapped final 6h silently shifts the window.
- **BAC-7** (`volume_features._daily_bars`): `shift(1)/shift(3)` for the volume-change
  features — same gap-blindness class.

## Hypothesis
The strategy is a fade on a *seasoned* liquidity-migration event; the flagship gate keys
off a symbol's liquidity-rank improvement over the prior 7 calendar days. Across a data
gap, a positional `shift(7)` reaches a row 14+ calendar days back, so the measured
"7-day improvement" is over the wrong horizon and can flip the gate for a gapped symbol —
admitting a name that wasn't actually a 7-day migrant, or rejecting one that was. The fix
nulls the feature across a gap (no exact-calendar partner) rather than silently
mis-measuring it. For the dominant top-turnover universe (rarely gapped) the output is
**identical** — this is a no-op except on names with mid-history gaps, which are a
documented first-class concern (delist/relist; FHEUSDT-class archive holes).

## Predicted direction + magnitude
- **Incidence:** bounded — only symbols with a mid-history gap *and* a gap-spanning
  lookback at a candidate ts are affected. Top-turnover names are rarely sub-20-bar.
- **MAR / return Δ:** expected ~flat. The *risk* worth flagging: if in-sample MAR
  materially **improves** when gapped names are nulled, the prior edge partly came from
  gap-mismeasured selections (a methodology artifact); if it materially **degrades**, some
  real selections depended on the (incorrect) cross-gap lookback — investigate before
  accepting, do NOT auto-accept a number change as "alpha".
- **Trade count Δ:** small (a few gapped-symbol candidates gained/lost).
- **Falsifier:** |MAR Δ| > ~1.0 on either venue, OR a return sign flip, means the gate
  behaviour changed materially and the change must be understood (not rubber-stamped)
  before deploy.

## Roots that will be touched
- [x] bybit_full_pit (per-venue working dataset)
- [x] binance_full_pit (per-venue working dataset)
- [x] forward demo/paper (the LIVE short signal uses `_enriched_event_features`, so the
  deployed selection changes for gapped names once this ships — hence HELD until validated)

## Decision rule (a priori)
Run the deployed `promoted` profile on both venues pre- and post-fix.
- **Accept + deploy** iff: return sign unchanged on both venues; pooled MAR Δ within
  [−0.5, +∞); no new look-ahead (the fix only ever NULLS a feature, never invents one);
  the gap-handling unit tests pass.
- **Reject / investigate** iff: pooled MAR Δ < −0.5, OR a return sign flips, OR MAR
  *improves* by > +1.0 (suspicious — confirm it's not a different artifact).

## Run command
```bash
# Baseline (pre-fix) + treatment (post-fix), promoted profile, both venues:
bash scripts/equity_curves.sh --sleeves short --years 3
# or the cell wrapper for the full report + decision metrics:
scripts/volume_events_cell.sh --cell-id pit_gapfix --overrides 'PROFILE=promoted'
# Compare MAR / return / trade-count vs the pre-fix baseline at the parent commit.
# Full-PIT op note (STATE.md): SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8; ~23GB peak.
```

## Implementation status (code committed to audit/fixes-2026-06-03, HELD — not pushed)
- **Implemented + unit-tested (gap→null / last-6h-correct, no-op for contiguous):** the
  positional-shift sites in `_daily_return_frame` + `_enriched_event_features` (incl. the
  flagship `liquidity_rank` shifts) via a `_calendar_shift` helper; BAC-5 last-6-hours;
  BAC-7 volume-change shifts.
- **Rolling-window residual — NOW IMPLEMENTED + unit-tested (was the documented deferral):**
  every gap-blind `rolling_max/min/std/mean/sum(window=N).over("symbol")` on the daily grid
  is now a **calendar-aware temporal window** via polars `rolling_*_by("ts_ms", window=N days,
  closed=...)`. Chosen over the "reindex to a contiguous grid" alternative because it is
  surgical (only the rolling expressions change; the grid shape is untouched) and provably
  no-op for a contiguous series. The conversion rule is verified equivalent at the expression
  level: `shift(1).rolling_X(N, min_samples=M)` ≡ `rolling_X_by(N d, closed="left", min_samples=M)`
  (prior N days, excl. today) and `rolling_X(N, min_samples=M)` ≡ `rolling_X_by(N d, closed="right",
  min_samples=M)` (trailing N days, incl. today; `min_samples` made explicit because
  `rolling_*_by` defaults it to 1 while bare `rolling_*` defaults it to the window). Routed
  through a single `_cal_roll(...)` helper in `volume_events_features.py`. Sites converted:
  - `_daily_return_frame`: `prior20_close_high/low`, `close_to_high/low_7d`, `close_to_high_30d`,
    `prior30_max/min_daily_return`, `prior7_return_volatility`, `prior7_intraday_range_mean`.
  - `_enriched_event_features`: `prior3_volume_persistence_rank_min`,
    `prior7_volume_persistence_rank_max`, `prior7_abs_daily_return_mean`, `prior7_turnover_quote_mean`.
  - `_funding_feature_frame` (3d/7d sums + means), `_signed_flow_feature_frame` (3d sums),
    `_mark_index_basis_frame` / `_premium_index_frame` (3d/7d means),
    `_attach_market_context` (market 7d/30d sum + mean, single series, no `.over`).
  - `volume_features.build_volume_features`: `_roll3` / `_roll20_mean` (feed `volume_change_3d`
    and `volume_persistence`, **core selection scores**) — inline, window = `N*aggregation_ms`
    (interval-agnostic for Architecture-B).
  - Tests: `test_daily_return_frame_rolling_windows_are_calendar_not_row_based` and
    `test_volume_features_rolling_sum_is_calendar_not_row_based` (gap→null; contiguous unchanged),
    plus the full existing feature suite as the contiguous-equivalence gate (1259 passed).
  - **Scope expansion note:** the falsifier below was written for the flagship-shift change.
    These rolling features feed pattern scores and secondary gates, not the single flagship
    gate, so a slightly larger (still gapped-names-only) MAR Δ is plausible; the same
    accept/reject thresholds apply — a *material* change must be understood, not rubber-stamped.
- **LON-6 — IMPLEMENTED + numerical-equivalence test (cherry-pickable, pushable standalone):**
  the `fc_min_day` sweep now builds the read+feature panel ONCE (`build_long_research_inputs`)
  and reuses it across entry-only cells (`run_long_native_research(..., precomputed_inputs=)`);
  the default path is byte-identical and `test_run_long_native_research_precomputed_inputs_are_equivalent`
  asserts precomputed ≡ default. Committed on this branch above the alpha; touches no signal
  numerics, so it does not need this validation run.
- **LON-7 — NOT shipped (honest):** narrowing the stateful day-loop's `to_dicts` projection
  is the only remaining long-research perf lever, but it can't be proven numerics-safe without
  a FOMO-triggering fixture (the available fixture yields 0 trades), so it is documented, not
  shipped. LON-6 already captured the dominant per-cell re-read+rebuild cost.

## Post-run results
(fill in after the operator runs the validation; include report paths + the commit SHA.)

## Verdict
pending — HELD until the validating backtest above is run and the decision rule applied.
