# Pre-registration: liquidity-capacity filter + filter-tweak sweep

**Date:** 2026-05-28
**Author:** owner
**Stage:** EXPLORATORY

## What's changing

Sweep candidate parameter changes against the current promoted baseline
(threshold 0.4 / hold 3 / stop 0.12 / TP 0.26 / cost 3.0× /
universe_rank 31-400 / rank_improvement 150 / turnover_ratio 6.0 /
residual_return 0.08 / close_location 0.30 / pit_age 90 / crowding
union_pathology / max_active_symbols 3) on bybit_full_pit + binance_full_pit.

## Hypotheses

**H1 (operator concern):** The strategy enters low-liquidity "shitcoin"
names where Bybit's per-order `maxMktOrderQty` is binding. Live demo
recently entered REQUSDT (cap 20,000) and stopped at -12%. Skipping
symbols below a daily-turnover floor (a proxy for liquidity capacity
since the historical maxMktOrderQty is not in our archive) should reduce
drawdown by avoiding the venue's most fragile names without losing
meaningful return.

**H2:** Tightening `rank_improvement_min` (150 → 200 or 250) selects
only the most decisive liquidity migrations and could improve Sharpe at
the cost of fewer trades.

**H3:** Tightening `residual_return_min` (0.08 → 0.12 or 0.15) selects
only the most outlier moves, improving the signal-to-noise.

**H4 (already-known per `docs/research_findings.md` 2026-05-23 sweep):**
`hold_days=2` (vs 3) improves split-Sharpe by ~17% on the rebuilt data.
Confirmation pass + interaction tests with H1-H3.

**H5:** Tightening `universe_rank_max` (400 → 200 or 300) excludes the
weakest-liquidity universe tail.

**H6 (combos):** The above filters compound — e.g. liquidity floor +
h=2 + tighter residual return might stack additively.

## Predicted direction + magnitude

- **H1** (turnover floor): Sharpe Δ +0.0 to +0.3; trade Δ -10% to -40%;
  DD Δ -2pp to -5pp. Failure mode: if low-cap symbols are where the edge
  IS (small-cap reversal premium), removing them tanks return.
- **H2** (rank_improvement_min ↑): Sharpe Δ +0.0 to +0.2; trades -20% to -50%;
  DD similar.
- **H3** (residual_return_min ↑): Sharpe Δ ±0.2; trades -30% to -60%.
- **H4** (hold_days=2): Sharpe Δ +0.4 (per prior sweep); trades same; DD slightly worse.
- **H5** (rank_max ↓): Sharpe ±0.1; trades -20% to -40%.
- **H6** (combo): unknown — may stack or interfere.

## Roots that will be touched

- [x] bybit_full_pit (per-venue working dataset)
- [x] binance_full_pit (per-venue working dataset)
- [ ] forward demo/paper (no live config change from this sweep)

## Decision rule (a priori)

A cell qualifies as "candidate improvement" only if **all** hold:

1. **Sharpe Δ ≥ +0.5** vs baseline on both Bybit AND Binance independently
   (the +0.5 threshold deflates for the ~17-cell multiple-testing bar — at
   17 cells, Bonferroni-equivalent z-score 2.5 = ~+0.4 Sharpe; +0.5 buys
   headroom).
2. **Max DD ≤ baseline DD + 5pp** on both venues (no return improvement
   accepted if it costs material drawdown).
3. **Sign of return ≥ 0** on both venues (sign-stability).
4. **Trade count ≥ 30** on Bybit (statistical power floor).

If no cell qualifies, the recorded verdict is "**no improvement found**" —
keep the current production parameters.

If multiple cells qualify, pick the one with smallest parameter delta from
the current production (Occam's razor — small change less likely to be
overfit).

## Roots' coverage caveat

The bybit_full_pit rebuild (commit `dbb79fa`) excluded a small subset of
2021 ETHUSDT / ADAUSDT date partitions due to a manifest-extension gap that
predates this sweep — so the run uses `--allow-partial-pit`. This is
labelled biased per the integrity standard; the missing data is at the
oldest edge of the window (2021-01) and does not affect 2023-2026 entry
selection (when the strategy actually fires).

## Run command

```bash
bash scripts/run_liquidity_capacity_sweep.sh
```

Internally loops over 17 cells × 2 venues = 34 invocations of
`python -m liquidity_migration volume-events --explain-rejections=False
... --start 2024-01-01 --end 2026-05-28`.

Output: `~/SHARED_DATA/{bybit,binance}_full_pit/reports/sweep_2026-05-28/<cell-id>/`
plus a top-level `sweep_summary.csv` aggregating Sharpe / DD / return /
trade-count per cell per venue.

## Post-run results

(filled after run completes)

## Verdict

(filled after analysis)

## Honesty notes

- Every cell here is **EXPLORATORY** by the integrity-skill labelling.
- The rebuilt data root already inherited prior parameter-sweep wear
  (the strategy was historically tuned on similar data). Any improvement
  found here is subject to additional multiple-testing inflation beyond
  what the +0.5 Sharpe gate compensates for.
- A "no improvement found" verdict is the most likely outcome and is
  itself useful evidence — the production strategy has been near a local
  optimum for a while.
- Cross-venue agreement (Bybit AND Binance) is the only robustness signal
  available without burning pristine OOS.
