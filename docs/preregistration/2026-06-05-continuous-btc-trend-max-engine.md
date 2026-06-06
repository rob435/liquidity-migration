# Pre-Registration: Continuous BTC-Trend MAX Engine

Date: 2026-06-05

Status: PRE-REGISTERED before the execution-grade run.

## Objective

Test whether the continuous liquidity-migration fade becomes execution-credible
when the causal residual-momentum pool is restricted to the same BTC 30-day trend
regime concept used by the deployed daily short baseline.

This follows the exploratory feature scout only. The scout is not promotion
evidence.

## Data Roots

- Bybit: `~/SHARED_DATA/bybit_full_pit`
- Binance: `~/SHARED_DATA/binance_full_pit`
- Window: 2023-04-01 through 2026-05-28
- Residual momentum: causal `residual_momentum.parquet`, rebuilt with shift(3)

## Frozen Grid

- Venues: Bybit, Binance
- Feature sets:
  - `max_ret168`
  - `rv_168h,max_ret168`
- Residual-momentum quantiles: `0.25`, `0.33`
- Liquidity gate: hourly `turnover_quote >= 1,000,000`
- Hold: `24h`
- Exit mode: `fixed`
- Entry delay: `1` hour after the deciding closed bar
- BTC trend gates:
  - `uptrend`: prior 30 daily BTC returns, excluding the signal day, sum `> 0`
  - `downtrend`: prior 30 daily BTC returns, excluding the signal day, sum `<= 0`
- Constructions:
  - short-only D9
  - D9 short leg
  - D0 long leg
  - market-neutral D0/D9
- Costs: existing continuous execution model, including funding and impact
  (`impact_coef_bps=50`, `deploy_capital_usd=1,000,000`)

## Baseline

The benchmark is the deployed daily short profile from `promoted.short_profile()`,
run over the same venue and date window with the standard cost config.

## Acceptance Gate

A candidate may advance only if the execution-grade run shows:

- positive MTM total return for the candidate construction on both venues,
- positive realized early and recent split returns on both venues,
- correlation to the daily baseline below `0.45` in absolute value on both venues,
- MAR beats the deployed daily short on both venues, or pooled MAR delta is
  positive with no venue worse than `-0.5` MAR,
- no obviously pathological drawdown, skip, or trade-count pattern in the reports.

If no cell passes this gate, the result is a rejection, not evidence for
deployment.
