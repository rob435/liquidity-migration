# Pre-Registration: Hourly Event-Triggered MAX Short Overlay

Date: 2026-06-05

Status: PRE-REGISTERED before the execution-grade run.

## Objective

Test whether the continuous MAX overlay can be made genuinely hourly
event-driven: enter only after a fresh hourly catalyst, not on every fresh
cross-sectional decile spell.

This is a short-only overlay test. The D0 hedge used in the proxy scout was
timestamp-paired and is not yet implemented in the execution engine, so it is
not part of this pre-registered run.

## Frozen Candidate Set

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
  - `fresh_pop10`: current hourly return is at least `10%` and is the fresh
    trailing-168h max hourly return
  - `pop10_gb1`: prior 6h max hourly return at least `10%`, current hourly
    return non-positive, and close gives back at least `1%` from prior 6h high
  - `turn5_pop3`: current turnover at least `5x` prior 168h mean and current
    hourly return at least `3%`
- Costs: existing continuous execution model, funding and impact included
- Combined-book check: add the event-triggered short-only overlay at scale `1.0`
  to the deployed daily short baseline.

## Acceptance Gate

An event-triggered candidate may advance only if:

- standalone short-only overlay has positive MTM total return on both venues,
- trade count is less than half the prior non-event MAX/BTC-uptrend overlay
  trade count on both venues,
- combined daily+overlay MAR beats deployed daily short MAR on both venues,
- combined total return beats deployed daily short total return on both venues,
- combined absolute max drawdown is no more than `1.10x` the daily baseline on
  both venues.

Passing this check is research evidence only. It is not a real-money promotion.
