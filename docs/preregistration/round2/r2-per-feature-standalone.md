# R2 — Per-feature standalone decile-sort + correlation matrix (pre-registration)

**Date:** 2026-05-28
**Stage:** proposed (committed with code, not yet run).
**Parent plan:** [2026-05-29 Round 2 integrated-strategy program](integrated-strategy-program.md), section "Sub-phase R2".
**Phase label per integrity standard:** `exploratory` — R2 outputs are descriptive; no individual-feature cell graduates to R10 alone. R2 feeds R9's integrated-strategy assembly via the correlation matrix and PCA decomposition.

## Purpose

Round 1 Phase 5 identified 5 features with stable cross-venue IC at fwd_ret_3d. Phase 6 jumped to combination before measuring standalone P&L or feature correlations. R2 does the missing work:

- Per-feature standalone decile-sort backtest at 3 horizons × 2 venues.
- 5×5 Spearman correlation matrix on the daily decile-spread P&L per venue.
- PCA decomposition to test the plan's strong hypothesis that the 5 features collapse to ~2 orthogonal factors (Factor A: vol/extension state; Factor B: short-horizon momentum reversal).

R2 is **infrastructure** for R9, not a candidate-generator for R10. The Investigation-tier verdict on each cell is reported for transparency but doesn't queue any cell for promotion.

## Features under test (5)

The 5 cross-venue-IC survivors from Round 1 Phase 5 (all with negative IC = short signal on the top decile):

| Feature | Phase 5 mean \|IC\| | Mechanism hypothesis |
|---|--:|---|
| `vol_of_vol_30d` | 0.087 | Vol-of-vol = regime instability; high-vov names mid-pump-cycle |
| `realized_vol_7d` | 0.081 | Low-vol-anomaly cross-section: high recent vol = overreaction state |
| `dist_from_30d_low` | 0.071 | Extended from base = overbought; short-horizon mean reversion |
| `xs_rank_ret_7d` | 0.043 | Short-horizon momentum reversal at 7d |
| `xs_rank_ret_3d` | 0.039 | Same at 3d (likely correlated with 7d) |

## Decile-sort backtest specification

For each (feature × horizon × venue) cell:

1. Build a feature panel with the 5 features + `fwd_ret_{1,3,7}d` columns using `signal_harness.build_feature_panel()` (existing).
2. Each day, rank symbols by feature value cross-sectionally.
3. Identify the top decile (top 10% by feature value) → these are short candidates (because all 5 features have negative IC).
4. Enter short positions on each name at the next bar (matches the +1h entry-delay convention; fwd_ret_Nd already accounts for the +1h fill).
5. Hold for `horizon` days. P&L per signal = `-fwd_ret_horizon_d` (negative because we're short).
6. Position sizing: 1/realized_vol_7d per name, target vol per name = 1.0% daily, clipped to [0.001, 10] to bound 1/vol blow-ups. This anticipates R5; for R2 we use a fixed target_vol = 0.01.
7. Cost model: legacy `cost_multiplier = 3` flat per round-trip. Cost subtracted from per-signal P&L as `2 * cost_multiplier * 1e-4` (3 bps each side = 6 bps round-trip; cost_multiplier 3 = 18 bps round-trip total). For R6 re-cost downstream.
8. Daily P&L is the sum over the day's new entries of `(-fwd_ret_horizon_d - round_trip_cost) * size_factor`. This realizes the entire holding-period P&L on the signal date (a simplification valid for descriptive analysis, NOT for live deployment timing).
9. Cumulative equity curve, max drawdown, Sharpe (annualized), total return computed from the daily P&L series.
10. Trade count = number of (symbol, date) signal entries.

The choice "realize P&L on signal date" makes per-feature standalone analysis comparable across horizons without overlapping-position accounting. R9 (integrated strategy) uses proper position-lifecycle accounting; R2 is descriptive.

## Cells (30 standalone + 2 correlation matrices = 32 deliverables)

| Cell ID | Feature | Horizon | Venue | Sweep |
|---|---|--:|---|---|
| `R2_vol_of_vol_30d_h1_bybit` | vol_of_vol_30d | 1 d | bybit | sweep |
| `R2_vol_of_vol_30d_h3_bybit` | vol_of_vol_30d | 3 d | bybit | sweep |
| `R2_vol_of_vol_30d_h7_bybit` | vol_of_vol_30d | 7 d | bybit | sweep |
| `R2_realized_vol_7d_h1_bybit` | realized_vol_7d | 1 d | bybit | sweep |
| `R2_realized_vol_7d_h3_bybit` | realized_vol_7d | 3 d | bybit | sweep |
| `R2_realized_vol_7d_h7_bybit` | realized_vol_7d | 7 d | bybit | sweep |
| `R2_dist_from_30d_low_h1_bybit` | dist_from_30d_low | 1 d | bybit | sweep |
| `R2_dist_from_30d_low_h3_bybit` | dist_from_30d_low | 3 d | bybit | sweep |
| `R2_dist_from_30d_low_h7_bybit` | dist_from_30d_low | 7 d | bybit | sweep |
| `R2_xs_rank_ret_7d_h1_bybit` | xs_rank_ret_7d | 1 d | bybit | sweep |
| `R2_xs_rank_ret_7d_h3_bybit` | xs_rank_ret_7d | 3 d | bybit | sweep |
| `R2_xs_rank_ret_7d_h7_bybit` | xs_rank_ret_7d | 7 d | bybit | sweep |
| `R2_xs_rank_ret_3d_h1_bybit` | xs_rank_ret_3d | 1 d | bybit | sweep |
| `R2_xs_rank_ret_3d_h3_bybit` | xs_rank_ret_3d | 3 d | bybit | sweep |
| `R2_xs_rank_ret_3d_h7_bybit` | xs_rank_ret_3d | 7 d | bybit | sweep |
| `R2_*_*_binance` (mirror 15) | (5 features × 3 horizons) | — | binance | sweep |
| **`R2_correlation_matrix_bybit`** | (5×5 Spearman on h3 daily P&L) | — | bybit | derived |
| **`R2_correlation_matrix_binance`** | (5×5 Spearman on h3 daily P&L) | — | binance | derived |

5 features × 3 horizons × 2 venues = 30 sweep cells + 2 derived correlation matrices.

## Window

**2023-04-01 → 2026-04-30** (1125 days; cross-venue minimum, matches R1).

Sub-period thirds: 2023-04-01 → 2024-04-30, 2024-05-01 → 2025-04-30, 2025-05-01 → 2026-04-30.

## Decision rule — Round 2 Investigation tier (descriptive only)

The Investigation-tier verdict applies per-cell, **for documentation only**. No cell graduates to R10's candidate queue from R2 individually — R9 consumes R2's correlation matrix + PCA to decide on the integrated-strategy combination scheme.

A cell is investigation-positive if:
- MAR Δ > 0 on majority of venues (not applicable to R2 cells since each cell is venue-specific; instead we report each cell against the venue-specific "do-nothing" P&L of 0)
- Equivalent test for R2: `MAR > 0 on the venue` (the cell stands on its own)
- ≥ 30 distinct signal days on Bybit (≥ 20 on Binance) — looser than R1's 30/20 since this is per-feature, not per-strategy

For R2's special structure, the analyzer reports `mar` (absolute, not delta) and labels:
- `positive`: MAR > 0 AND ≥ 30 signal days
- `marginal`: MAR > 0 but < 30 signal days
- `negative`: MAR ≤ 0

These labels are descriptive; the verdict commit summarizes how the 5 features compare for R9's combination scheme.

## Correlation matrix

For each venue, compute the 5×5 Spearman correlation matrix on the **daily decile-spread P&L** at horizon = 3 days (Phase 5's strongest IC horizon).

The plan's strong hypothesis: 5 features collapse to ~2 orthogonal factors:
- **Factor A: "vol/extension state"** = `vol_of_vol_30d` + `realized_vol_7d` + `dist_from_30d_low`
- **Factor B: "short-horizon momentum reversal"** = `xs_rank_ret_7d` + `xs_rank_ret_3d`

If the matrix confirms this clustering (intra-cluster ρ ≥ 0.4, inter-cluster ρ ≤ 0.2), R9 uses the factor structure. Otherwise R9 weights all 5 features by IC × diversification.

## PCA decomposition

Per venue, compute PCA on the 5-feature daily P&L matrix. Report variance explained by:
- PC1 alone
- PC1 + PC2 (target: ≥ 80%)
- PC1 + PC2 + PC3
- PC1–5 (trivially 100%, sanity check)

If PC1 + PC2 ≥ 80%, R9 has clean confirmation of the 2-factor structure.

## Roots that will be touched

- [x] `~/SHARED_DATA/bybit_full_pit`
- [x] `~/SHARED_DATA/binance_full_pit`
- [ ] forward demo/paper (no — pure backtest)
- [ ] pre-2023 OOS window (no — reserved for R11)

## Code changes required for R2

1. **New module:** `liquidity_migration/r2_decile_sort.py`
   - `decile_spread_pnl(panel, feature, horizon, *, top_decile, vol_target_per_name, cost_multiplier_round_trip, realized_vol_col) -> pl.DataFrame`
     Returns columns: `date`, `n_signals`, `daily_pnl`, `cum_return`, `drawdown`.
   - `summarize_pnl_series(pnl_frame, *, window_days) -> dict`
     Returns: `total_return`, `annualized_return`, `max_drawdown`, `sharpe_like`, `mar`, `n_signal_days`, `total_signals`.
   - `spearman_correlation_matrix(pnl_by_feature, feature_names) -> pl.DataFrame`
     Returns N×N matrix (with feature names as both axes).
   - `pca_variance_shares(pnl_by_feature, feature_names) -> dict`
     Returns `{"explained_variance_ratio": [v1..vN], "cumulative_variance": [c1..cN]}`.

2. **New orchestrator:** `scripts/r2_per_feature_standalone_sweep.py`
   For each venue: build feature panel once, loop over (feature × horizon), call `decile_spread_pnl` + `summarize_pnl_series`, write per-cell row to summary CSV. After all cells: compute correlation matrix + PCA and write to a derived JSON artifact.

3. **Tests:** `tests/test_r2_decile_sort.py`
   - Decile-spread P&L on a synthetic panel with known IC produces expected sign and magnitude.
   - Correlation matrix on perfectly-correlated synthetic features ≈ 1.0; on independent features ≈ 0.
   - PCA on a 2-factor synthetic structure recovers ~50/50 variance split.
   - summarize_pnl_series math sanity (sharpe = mean/std × sqrt(N), MAR = annualized / |dd|).

Effort estimate: ~3 hours code + tests. Cleanly additive.

## Compute budget

Per venue:
- Feature panel build: ~3 min (5-year window, polars-native)
- 15 (feature × horizon) cells × ~30 sec/cell = ~7-8 min
- Correlation matrix + PCA: ~10 sec

Total: ~10-12 min per venue, ~25 min wall both venues sequential, ~15 min at 2-way parallel.

## Dispatch

```powershell
$env:POLARS_MAX_THREADS = "8"
.venv\Scripts\python.exe scripts\r2_per_feature_standalone_sweep.py

.venv\Scripts\python.exe -X utf8 scripts\apply_decision_rule.py `
  $env:USERPROFILE\SHARED_DATA\r2_per_feature_standalone_2026-05-28_summary.csv `
  --control __none__ `
  --rule investigation
```

(For R2 the control concept is `__none__` — each cell stands on its own. The analyzer falls back gracefully when `--control` is absent in the CSV; see analyzer changes below.)

**Analyzer extension required:** the existing `apply_decision_rule.py` assumes a control cell exists. For R2's per-feature standalone structure there is no control; each cell is compared against `MAR > 0` directly. We add a `--no-control` mode that:
- Skips control-cell discovery and Δ computation
- Applies the looser absolute thresholds (MAR > 0, ≥ 30 signal-days Bybit / ≥ 20 Binance)
- Labels cells: `positive`, `marginal`, `negative`

Cleanly additive to the analyzer; existing `--control X` mode unchanged.

## Pre-commitments

1. **No individual R2 cell graduates to R10.** R2 is descriptive infrastructure for R9.
2. **Correlation thresholds pre-committed:** intra-cluster ρ ≥ 0.4 + inter-cluster ρ ≤ 0.2 → use 2-factor structure in R9. Otherwise fall back to IC-weighted all-5.
3. **PCA threshold pre-committed:** PC1 + PC2 ≥ 80% confirms 2-factor structure.
4. **Per-feature sub-period stability is reported, not gated.** R2 reports each feature's MAR across the 3 sub-period thirds for informational use; we do NOT gate the verdict on sub-period consistency (this is descriptive, after all).
5. **No off-menu features.** If R2 surfaces a hypothesis about a 6th feature, it requires a separate pre-reg.

## Threats to inference

| # | Threat | R2 mitigation |
|---|---|---|
| #1 | Future universe selection | Full PIT roots; cross-venue minimum window |
| #2 | Future info in signals | All features end-of-day-close causal; fwd_ret_Nd defined as `(close_D+N - first_bar_close_D+1) / first_bar_close_D+1` — matches the +1h entry-delay model used in production |
| #4 | Revised / non-PIT data | Full PIT roots; idempotent rebuild scripts |
| #15 | Warm-started state | Decile-sort is stateless per-day; no warm-up needed |
| #17 | Parameter mining | Decile cutoff fixed at top 10% pre-commitment; horizons {1, 3, 7} pre-registered |
| #18 | OOS reuse | Pre-2023 window untouched |
| #19 | Multiple testing | 30 cells × 2 reports per cell, BUT — R2 cells don't promote individually; FDR ceiling doesn't bind here. The correlation/PCA hypothesis (2-factor) is ONE pre-committed test, not 30 |
| #20 | Bad accounting | P&L realized on signal date is documented simplification; R9 uses proper position-lifecycle accounting |
| #23 | Pretty-report bias | Every cell produces per-day P&L series CSV + summary metrics + decile-spread time series JSON |
| #25 | All-or-nothing compute | Sweep flushes per-cell summary to CSV after each cell |

## Forward pointer

- **R3 (bearish stack honest test):** not gated by R2; runs in parallel.
- **R9 (integrated strategy):** consumes R2's correlation matrix + PCA decomposition to decide combination scheme. Blocked on R2 completion.
- **R5/R6 (sizing + cost model):** R2's `vol_target_per_name = 0.01` and `cost_multiplier_round_trip = 3` are placeholder values; R5/R6 will replace them. R2 cells are NOT re-run after R5/R6 — the descriptive infrastructure conclusions hold up to scale.

## Open questions before dispatch

1. **Decile boundary at top 10% — strict vs inclusive?** I'll use `signal_rank_frac ≤ 0.10` (strict), matching the convention in `build_combined_signal_portfolio`. Could be `< 0.10` if operator prefers; difference is one name per day at the boundary.
2. **Correlation matrix at h=3 only, or at all 3 horizons?** Per the plan, h=3 only (the strongest Phase 5 IC). If operator wants all 3 horizons, easy amendment.
3. **PCA on what frame — h=3 daily P&L only?** Same as #2. Pinned at h=3.
