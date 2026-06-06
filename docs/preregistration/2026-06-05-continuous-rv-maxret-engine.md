# Pre-registration: continuous rv+max-ret engine follow-up

**Date:** 2026-06-05
**Author:** Codex
**Stage:** run-pending

## What's changing

Add an engine-grade follow-up for the exploratory causal feature scout: continuous cross-sectional sorting by high trailing volatility (`rv_168h`) plus high trailing one-hour lottery return (`max_ret168`) instead of the old five-feature composite.

## Hypothesis

The invalidated continuous sleeve mixed several broad features and relied on a leaky rmom panel. After the causal `shift(3)` fix, the broad D9/D0 composite is weak. The exploratory scout shows the strongest causal, cross-venue, both-era object is more specific: liquid names with low causal residual momentum and extreme recent volatility/lottery-demand signatures. Mechanism: these are crowded post-pump names whose lottery bid is exhausted; the short leg should fade, while the low-score leg is a stabilizer rather than the source of alpha.

## Predicted direction + magnitude

- Engine-grade L/S should be positive on both venues, with 24h fixed hold stronger than 12h.
- The direct D9 short may carry most of the edge; L/S is accepted only if it improves drawdown or correlation versus short-only.
- Compared with the old broad continuous composite, MAR and total return should improve materially on Binance; otherwise the scout was a proxy artifact.
- Failure mode: engine timing, churn/cost, or intrahold MTM erases the proxy edge.

## Roots that will be touched

- [x] `~/SHARED_DATA/bybit_full_pit`
- [x] `~/SHARED_DATA/binance_full_pit`
- [ ] forward demo/paper

## Frozen cells

All cells use causal rebuilt `residual_momentum.parquet`, `feature_set=(rv_168h,max_ret168)`, `entry_delay_hours=1`, full-PIT roots, funding-to-exit, size/ADV impact, and portfolio MTM metrics.

- `rmom_quantile in {0.25,0.33,0.50}`
- `liq_turnover_min = 1000000`
- `hold_hours in {12,24}`
- `exit_mode = fixed`
- D9 short-only, D0 long-only, and equal-gross D0/D9 L/S
- Cost stress only if the base cell clears: `impact_coef_bps in {75,100}` and `deploy_capital_usd=3000000`

## Decision rule (a priori)

Accept for a larger cost/capacity run only if:

- L/S and short-only are net-positive on both venues.
- At least one construction beats the deployed daily short MAR on both venues, or beats pooled MAR with neither venue worse by more than 0.5 MAR.
- Early and recent realized returns are both positive on both venues.
- MTM DD and worst-day are believable; no sub-5% DD with thousands of trades is accepted without hourly-tail review.
- Correlation to the daily short is reported; if the direct short wins but correlation is high, it is a capacity/return add-on, not a diversifier.

If it fails, reject this feature-set lead and do not tune more two-feature composites without a new pre-registration.

## Run command

```powershell
.venv\Scripts\python.exe scripts\continuous_causal_rmom_vs_daily.py `
  --rmom-quantiles 0.25,0.33,0.50 `
  --liq-turnover-mins 1000000 `
  --hold-hours 12,24 `
  --exit-modes fixed `
  --feature-set rv_168h,max_ret168 `
  --out $HOME\SHARED_DATA\cont_rv_maxret_2026-06-05
```

## Post-run results

To be filled after the run.

## Verdict

Pending.
