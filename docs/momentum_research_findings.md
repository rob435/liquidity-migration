# Cross-Sectional Momentum — First Research Findings

**Date:** 2026-05-23
**Status:** First runs on canonical research root. All runs `exploratory`.
**Spec:** [cross_sectional_momentum_proposal.md](cross_sectional_momentum_proposal.md)
**Code:** `liquidity_migration/{momentum_signals,momentum_events,cross_sectional_momentum}.py`

## TL;DR

- Default config (all 5 entry conditions) fires only **5 trades in 3 years** —
  too sparse to be useful. Total return −2.54%, Sharpe −0.62.
- Funnel diagnostic identified **two binding constraints**: the breakout filter
  (kills 97% of post-rank candidates) and the coil-release filter (kills 96%
  of remaining). Stacked, they reject 99.97%.
- **Dropping the breakout requirement** produced 33 trades, +13.40% total
  return, Sharpe 0.60, max DD −7.60%, with all three splits positive. The
  mechanical promotion gate passes.
- **Dropping the coil-release requirement** produced 86 trades, **−18.22%
  return, max DD −35.24%** — confirming coil-release is doing useful work and
  preventing late entries.
- **The no-breakout variant cannot be called a candidate.** It was selected
  post-hoc from a 2-variant sweep. Integrity gate #17 (parameter mining) says
  this label belongs to a pre-registered configuration validated OOS, not the
  winner of any sweep.

## Run 1 — Default (all 5 conditions)

Command:
```
python -m liquidity_migration cross-sectional-momentum \
  --start 2023-05-03 --end 2026-05-18
```

Report: `~/SHARED_DATA/bybit_fullpit_1h/reports/cross_sectional_momentum_research/`

| Metric | Value |
|---|---|
| Run label | `full_pit_universe` |
| Feature rows | 368,421 |
| Entry candidates | 5 |
| Trades | 5 |
| Total return | −2.54% |
| Max drawdown | −5.74% |
| Sharpe-like | −0.62 |
| Funding mode | modeled |
| Promotion | **False** (not all splits positive) |

The 5 trades, in order: BNBUSDT (Mar 2024, +1.24% / vol shock), STXUSDT (Mar
2024, −0.66% / universe demotion), POPCATUSDT (Nov 2024, **−5.12% / trend
break** — the dominant loss), 1000PEPEUSDT (May 2025, +1.59% / vol shock),
ETHUSDT (Aug 2025, +0.53% / vol shock).

### Funnel diagnostic

Per (date, symbol) filter cascade in the 2023-05-04 → 2026-05-17 window:

| Filter | Rows | % of prior | % of total |
|---|---:|---:|---:|
| All feature rows | 247,539 | — | 100.0% |
| + in_liquidity_tier (top 30) | 15,939 | 6.4% | 6.4% |
| + rank_norm ≥ 0.75 | 4,163 | 26.1% | 1.7% |
| + regime_on (BTC > 200d SMA) | 3,212 | 77.2% | 1.3% |
| + breakout (close > 60d high) | 110 | **3.4%** | 0.044% |
| + funding not overheated | 92 | 83.6% | 0.037% |
| + coil_release | 4 | **4.3%** | 0.0016% |

The breakout and coil-release filters each kill ~97% of incoming rows. Coil
release events fire 2,535 times in window standalone; in tier they drop to 178;
in tier + breakout to ~3.

## Run 2 — No breakout

Command:
```
python -m liquidity_migration cross-sectional-momentum \
  --start 2023-05-03 --end 2026-05-18 \
  --no-breakout \
  --report-dir ~/SHARED_DATA/bybit_fullpit_1h/reports/cross_sectional_momentum_no_breakout
```

| Metric | Value |
|---|---|
| Entry candidates | 33 |
| Trades | 33 |
| Total return | +13.40% |
| Max drawdown | −7.60% |
| Sharpe-like | 0.60 |
| Trade win rate | 51.52% |
| Profit factor | 1.77 |
| Gross / cost / funding | +15.85% / −1.19% / −1.13% |
| Splits (train / val / oos) | +2.35% / +6.31% / +4.26% |
| Mechanical promotion | **True** |

Exit-reason breakdown:

| Reason | Count | Avg net return |
|---|---:|---:|
| trend_break | 16 | −0.92% |
| vol_shock | 12 | +1.88% |
| regime_break | 3 | +0.09% |
| rank_decay | 1 | +6.07% |
| universe_demotion | 1 | −0.66% |

Held 21 unique symbols. Median hold 23h, mean 155h (vol-shock exits skew low,
trend-break exits skew high).

## Run 3 — No coil-release

Command:
```
python -m liquidity_migration cross-sectional-momentum \
  --start 2023-05-03 --end 2026-05-18 \
  --no-coil-release \
  --report-dir ~/SHARED_DATA/bybit_fullpit_1h/reports/cross_sectional_momentum_no_coil
```

| Metric | Value |
|---|---|
| Trades | 86 |
| Total return | **−18.22%** |
| Max drawdown | **−35.24%** |
| Sharpe-like | −0.31 |
| Promotion | False |

Without coil-release, the strategy buys top-rank breakout coins indiscriminately
— late entries into already-extended moves. This validates that coil-release is
doing useful work as a timing filter.

## Findings

1. **The breakout filter, as currently specified, hurts more than it helps.**
   Requiring close > 60-day high after already filtering on top-quartile
   90-day Clenow slope means the strategy only buys when a coin is at the
   simultaneous high of two correlated lookback windows — i.e., near tops. The
   13.40% no-breakout return vs −2.54% baseline isn't subtle; this is a
   design flaw, not a calibration issue.

2. **Coil-release is load-bearing.** It's the only filter that keeps the
   strategy out of late, extended momentum names. Removing it triples the
   trade count and destroys 18% of capital in 3 years.

3. **The vol-shock exit fires too readily.** 12 of 33 winners (avg +1.88%) get
   cut on day 1 by `|log_return| > 3 × trailing_30d_median_abs_return`. In
   crypto, the trailing median absolute daily return is tiny (~1%), so 3× is
   ~3% — and a perfectly normal post-entry +5% surge triggers exit. This
   capped what could have been larger gains. The 3× multiple was the
   equities-literature default; needs a crypto-specific recalibration.

4. **All 33 no-breakout trades use `funding_mode = modeled`** — the canonical
   root has full funding coverage in this window, and the −1.13% funding
   return is a real cost being captured.

5. **Per-split distribution looks healthy** in the no-breakout variant:
   train +2.35%, validation +6.31%, OOS-internal +4.26%. The validation slice
   (2024-2025) carries the most trades (21) — consistent with that being the
   peak of the late-2024 / early-2025 alt-season.

## What this is NOT

- **Not a promotion candidate.** The no-breakout config was selected from a
  2-variant sweep post-hoc. Integrity gate #17 (parameter mining) and #18 (OOS
  reuse) require pre-registration before OOS validation. Treating the
  `promotion_gate_pass = True` as evidence is exactly the bias the standard
  exists to prevent.
- **Not OOS-validated.** The canonical root is in-sample by design; the splits
  are internal walk-forward, not true OOS. The pre-2023 Bybit and Binance OOS
  roots are still untouched on this strategy.
- **Not a real-money plan.** Deployment requires the OOS validation step
  below, then the demo daemon integration, then a paper/demo period.

## Next steps (pre-registered)

The honest sequence per the spec and methodology standard:

1. **Pre-register the no-breakout config as the v1 candidate.**
   - `require_breakout = False`
   - Everything else default.
   - Document this commitment in a dated note before running OOS.

2. **Run OOS validation on both pre-2023 roots.**
   - `~/SHARED_DATA/bybit_oos_pre2023` — funding-missing label.
   - `~/SHARED_DATA/binance_oos_pit` — funding-missing label.
   - **Do not tune.** Run with the pre-registered config exactly once. The
     spec's promotion gate (splits positive, max DD, Sharpe) is the test.

3. **If OOS holds:** investigate the vol-shock exit threshold. Specifically
   whether 4× or 5× outperforms 3×. This is a **single hyperparameter** sweep
   on a single dimension — limited multiple-testing exposure. Re-validate on
   OOS after.

4. **If OOS does not hold:** stop and document. Do not iterate on conditions
   to find a window where it survives — that's the parameter mining trap.

5. **Funding for OOS roots is missing.** Either accept the
   `funding-missing` label on OOS, or build funding backfill for both roots
   as separate data-infra work before step 2.

6. **The breakout requirement may still help in a different specification.**
   Worth exploring later: breakout against a shorter window (20-day instead of
   60-day) or breakout as an OR with coil-release rather than AND. Both are
   v2+ work; not for the v1 OOS run.

## Artifacts

```
docs/cross_sectional_momentum_proposal.md          # design spec
docs/momentum_research_findings.md                 # this file
~/SHARED_DATA/bybit_fullpit_1h/reports/cross_sectional_momentum_research/         # baseline run
~/SHARED_DATA/bybit_fullpit_1h/reports/cross_sectional_momentum_no_breakout/      # no-breakout
~/SHARED_DATA/bybit_fullpit_1h/reports/cross_sectional_momentum_no_coil/          # no-coil-release
```

Each report directory contains: `*_research_report.md`, `*_research_report.json`,
`*_trades.csv`, `*_baskets.csv`, `*_equity.csv`, `*_monthly.csv`. All reports
are reproducible from `run_label` + the config hash in the JSON metadata.
