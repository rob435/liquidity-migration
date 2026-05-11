# Current Bybit Research Spec

This file is intentionally short. It replaces the old composite
aggression/carry/momentum/live-runtime blueprint.

Read this before changing strategy, data ingestion, feature engineering, or
backtesting logic.

## Current Source Of Truth

The active alpha lead is documented in:

```text
docs/daily_close_fade.md
docs/profit_protection_audit_20260508.md
docs/backtesting_errors_we_never_repeat.md
configs/volume_alpha.default.yaml
```

The active contract is:

```text
Strategy: full-listing daily-close low-cap scam-tail short fade
Signal: 23:00 UTC
Ranking: day-to-date return using only completed bars available at 23:00
Entry: 60 equal 1m slices over [23:00, 00:00)
Entry price: average filled slice price
Stop: 8% above average entry, active immediately from first fill
Take profit: 10% below average entry
Adaptive exits: active from final add + 240m
Adaptive state: starts at activation, not from pre-activation lows/MFE
MFE giveback: after 3% favorable excursion, give back 50% from active-state MFE
Exit: flatten whole symbol
Re-entry: none in the same symbol/date
Universe: point-in-time Bybit public-trade archive listing manifest, prior 7d
  liquidity ranks 226+, excluding current top-cap/category-leader alpha coins
Mode: research/backtest first, not real-money live trading
```

Current-top-universe backtests are benchmarks only. The old +16k current-top
benchmark used legacy warm-started profit protection and is not promotable.
Promotion requires point-in-time archive validation and corrected exit
semantics.
It also requires passing the permanent backtesting-error standard in
`docs/backtesting_errors_we_never_repeat.md`.

Current selected full-listing artifacts:

```text
Config: configs/daily_close_fade.lowcap_scam_tail_selected.yaml
Report: data/volume_alpha/reports/daily_close_fade_full_listing_scam_tail_stage4_time_decay_selected/
Write-up: docs/daily_close_fade_full_listing_scam_tail_stage4_20260510.md
```

## Deprecated Scope

Do not rebuild the old system in this repo:

```text
SignalEngine
execution.py live order loop
state.py runtime state machine
alerting.py live Telegram bot behavior
runtime_monitor.py / runtime_validation.py
old aggression/carry/momentum/OI composite stack
real-money exchange submission
systemd/cron/deployment wrappers for demo or live order submission
```

Bybit demo plumbing may exist for execution testing, but demo fills are not
alpha proof.

## Bybit Data Notes We Still Need

Use Bybit V5 USDT linear perps:

```text
category=linear
settleCoin=USDT
status=Trading
exclude prelisting contracts
paginate instruments-info
```

Useful public endpoints:

```text
GET /v5/market/instruments-info
GET /v5/market/kline
GET /v5/market/tickers
GET /v5/market/funding/history
GET /v5/market/open-interest
GET /v5/market/recent-trade
WebSocket publicTrade.{symbol}
Bybit public trade archives
```

Important caveat: Bybit V5 klines provide OHLCV and turnover, but not
taker-buy/taker-sell volume. Any future taker-aggression signal must use signed
public trades, not candles alone.

For the current daily-close fade, 1m klines are sufficient to test the
top-gainer ranking, TWAP entry model, and exit lifecycle. The proof-grade path
should derive those 1m bars from Bybit public trade archives so delisted
symbols and historical symbol/date membership are preserved.

## Data Layout

Research data stays under a configurable data root:

```text
klines_1m/date=YYYY-MM-DD/symbol=SYMBOL/part.parquet
klines_1h/date=YYYY-MM-DD/symbol=SYMBOL/part.parquet
instruments/date=YYYY-MM-DD/part.parquet
archive_trade_manifest/date=YYYY-MM-DD/part.parquet
reports/
```

Use Polars and Parquet for research data. Keep large generated datasets out of
git.

## Promotion Gate

Do not call the alpha confirmed until:

1. Point-in-time Bybit archive membership is complete.
2. Archive-derived 1m bars cover the eligible universe for each day.
3. The unchanged 22:00-23:00 TWAP contract survives train, validation, and OOS
   windows.
4. Forward paper/demo implements real slice-level TWAP accounting.
5. Forward audit compares expected slices, demo orders, fills, slippage, missed
   trades, sleeve attribution, and daily PnL.

## Secondary Research

The daily volume-rank alpha remains secondary background:

```text
docs/volume_alpha.md
```

Do not blend it with the daily-close fade until each standalone alpha clears
costs and point-in-time validation.
