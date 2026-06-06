# Pre-registration: continuous causal-rmom vs daily short

**Date:** 2026-06-05
**Author:** Codex
**Stage:** run-pending

## What's changing

Revalidate the continuous liquidity-migration signal after the causal residual-momentum `shift(3)` fix, then compare the best causal continuous construction directly against the deployed daily short baseline on the same per-venue full-PIT roots and window.

## Hypothesis

The old continuous D9/rmom sleeve was invalid because the rmom panel leaked future residual returns, not because the continuous fade mechanism is dead. A causal, liquid-universe continuous construction may still beat the daily short if it uses the broader off-close opportunity set while controlling the two old failure modes: rmom look-ahead and excessive churn/cost. The strongest prior is not short-only; it is a market-neutral D0/D9 overlay, because the direct short shares too much exposure with the daily short.

## Predicted direction + magnitude

- Causal `shift(3)` rmom will materially reshuffle the selected names versus old `shift(1)`, so old thresholds are not trusted.
- Short-only D9 is expected to be competitive on return but likely correlated with the daily short.
- Market-neutral D0/D9 has the best chance to improve MAR versus the deployed daily short by reducing drawdown rather than maximizing gross return.
- Failure mode if wrong: causal rmom removes the old edge, Binance sign-flips, costs overwhelm the off-close breadth, or the overlay is redundant with the deployed long sleeve.

## Roots that will be touched

- [x] `~/SHARED_DATA/bybit_full_pit`
- [x] `~/SHARED_DATA/binance_full_pit`
- [ ] forward demo/paper

## Frozen first grid

All runs use `start=2023-04-01`, `end=2026-05-28`, full-PIT roots, causal rebuilt `residual_momentum.parquet`, honest nonzero entry delay, funding-to-exit, size/ADV impact, and portfolio MTM metrics.

- Daily baseline: `liquidity_migration.promoted.short_profile(start,end)` via `run_volume_event_research`.
- Continuous short: D9 short, `rmom_quantile in {0.25,0.33,0.50}`, `liq_turnover_min in {500000,1000000}`, `hold_hours in {6,12,24}`, `exit_mode in {fixed,state}`, `entry_delay_hours=1`.
- Continuous long leg: D0 long with the same continuous parameters.
- Continuous L/S overlay: combine D0 long and D9 short at equal gross leg budget, measured as one additive fixed-capital book.
- Cost stress on the locked neighborhood only: impact coefficient `{50,75,100}` bps and deploy capital `{1000000,3000000}`.

No half-life timing cells are allowed in this run; that thesis is already rejected.

## Decision rule (a priori)

Accept as a continuous candidate only if all hold:

- Positive net return on both venues after costs and funding.
- Continuous L/S MTM MAR beats the deployed daily short MTM/engine MAR on both venues, or beats pooled MAR while neither venue is worse by more than 0.5 MAR.
- Continuous L/S correlation to daily short is below 0.45 on both venues.
- Early and recent splits are both positive on both venues.
- +1h entry is the primary result; any `entry_delay_hours=0` diagnostic is invalid for evidence.
- The result survives the locked-neighborhood cost stress with positive net return both venues.
- Trade count and turnover are economically plausible; if capacity/impact assumptions explain the edge, reject.

If the causal continuous L/S fails this rule, the run is rejected. Do not re-label a failed short-only or single-venue result as alpha.

## Run command

```powershell
# 1) Rebuild causal rmom and clear stale continuous caches.
.venv\Scripts\python.exe scripts\precompute_residual_momentum.py --root $HOME\SHARED_DATA\bybit_full_pit --root $HOME\SHARED_DATA\binance_full_pit --start 2023-03-01 --end 2026-05-29
Remove-Item $HOME\SHARED_DATA\bybit_full_pit\_continuous_engine_panel_*.parquet -ErrorAction SilentlyContinue
Remove-Item $HOME\SHARED_DATA\binance_full_pit\_continuous_engine_panel_*.parquet -ErrorAction SilentlyContinue

# 2) Run the frozen grid and daily comparator.
.venv\Scripts\python.exe scripts\continuous_causal_rmom_vs_daily.py --out $HOME\SHARED_DATA\cont_causal_rmom_2026-06-05
```

## Post-run results

To be filled after the run. Include report paths, per-venue daily baseline metrics, continuous short metrics, continuous L/S metrics, split metrics, cost stress, and verdict.

## Verdict

Pending.
