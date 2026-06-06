# Pre-Registration: Sparse Hourly Event Trigger Follow-Up

Date: 2026-06-05

Status: PRE-REGISTERED before the execution-grade run.

## Objective

Refine the hourly event-driven MAX short overlay by testing sparser, higher
amplitude catalysts that the exploratory scout identified after the first
`fresh_pop10` result.

The objective is better combined-book quality with fewer trades, without
returning to an always-on continuous entry process.

## Frozen Grid

- Venues: Bybit, Binance
- Feature set: `max_ret168`
- Residual-momentum quantile: `0.25`
- Liquidity gate: hourly `turnover_quote >= 1,000,000`
- BTC trend gate: `uptrend`
- Side/construction: short-only D9
- Entry delay: `1` hour after the deciding closed bar
- Hold: `24h`
- Exit mode: `fixed`
- Event triggers:
  - `fresh_pop15`: current hourly return is at least `15%` and is the fresh
    trailing-168h max hourly return
  - `pop15_gb1`: prior 6h max hourly return at least `15%`, current hourly
    return non-positive, and close gives back at least `1%` from prior 6h high
  - `pop15_gb2`: same, but giveback at least `2%`
- Costs: existing continuous execution model, funding and impact included
- Combined-book check: add short-only overlay at scale `1.0` to deployed daily
  short baseline.

## Acceptance Gate

A trigger may advance only if:

- standalone short-only overlay has positive MTM total return on both venues,
- trade count is lower than the prior `fresh_pop10` event trigger on both venues,
- combined daily+overlay MAR beats deployed daily short MAR on both venues,
- combined total return beats deployed daily short total return on both venues,
- combined absolute max drawdown is no more than `1.10x` the daily baseline on
  both venues,
- 2x overlay-cost stress still passes the same combined-book MAR/total/DD gates.

Passing this check is research evidence only. It is not real-money promotion.
