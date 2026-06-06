# Pre-Registration: Daily Short + Continuous MAX Overlay

Date: 2026-06-05

Status: PRE-REGISTERED before the combined-book verification run.

## Objective

Test whether the deployed daily short baseline is improved by adding the best
execution-grade continuous overlay found in the BTC-trend MAX run.

This is a combined-book test, not a standalone continuous-sleeve promotion test.
The continuous sleeve remains research-stage.

## Frozen Candidate

- Baseline sleeve: deployed daily short from `promoted.short_profile()`
- Continuous overlay:
  - Feature set: `max_ret168`
  - Residual-momentum quantile: `0.25`
  - Liquidity gate: hourly `turnover_quote >= 1,000,000`
  - BTC trend gate: `uptrend`
  - Hold: `24h`
  - Exit: `fixed`
  - Construction: market-neutral D0/D9 L/S
  - Existing execution model: funding and impact included
- Overlay scale: `1.0`

## Inputs

Use the already generated execution artifacts from:

- `~/SHARED_DATA/cont_btc_trend_max_2026-06-05/max_ret168/bybit`
- `~/SHARED_DATA/cont_btc_trend_max_2026-06-05/max_ret168/binance`

The exact continuous cell is:

`q25_liq1000k_featmax_ret168_btcuptrend_h24_fixed_imp50_cap1m`

## Accounting

For each venue and date:

`combined_return = daily_short_return + 1.0 * continuous_ls_mtm_return`

The combined curve compounds the combined daily return stream. Missing sleeve
days contribute `0`. Report MAR with the same daily-PnL metric used by the
continuous harness and compare against the deployed daily short report.

## Acceptance Gate

The combined book passes this pre-registered check only if:

- combined MAR exceeds deployed daily short MAR on both venues,
- combined total return exceeds deployed daily short total return on both venues,
- combined absolute max drawdown is no more than `1.10x` the daily baseline
  absolute max drawdown on either venue,
- the report includes daily, overlay, and combined return artifacts for audit.

Passing this check would justify further stress tests and forward-demo planning
only. It does not promote the continuous sleeve to real money.
