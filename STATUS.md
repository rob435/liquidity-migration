# Status

## Current State

- Repo is a research lab, not a real-money live bot.
- Active lead is the daily-close short fade with 22:00-23:00 TWAP entry.
- Backtest TWAP is implemented.
- Forward/demo TWAP slicing is intentionally blocked until slice accounting and
  order execution are implemented.
- Pre-TWAP reports are historical only and should not drive new decisions.
- Volume-alpha remains a secondary research path, not the current default.

## Current Daily-Close Contract

```text
Signal: 22:00 UTC
Entry: 60 equal 1m slices over [22:00, 23:00)
Average entry: average filled opens
Hard stop: 20% above average entry, active from first fill + 15m
Profit protection: active from final add + 15m
Adaptive exits: 0.25x daily-vol trail, 20% MFE giveback after +1% MFE
Max hold: 180m after final add
Universe bucket: prior 7d baseline liquidity ranks 31-150
Sizing: score-capped, max 80% per symbol
```

## Latest Benchmark

```text
Dataset: current top-160 Bybit symbols
Range: 2023-05-15 to 2026-05-02
Trades: 750
Return: +16,896.41%
Sharpe-like: 10.63
Max DD: -15.39%
Worst day: -12.84%
Artifact root: data/research_reports/daily_close_twap_2200_2300_current_top160_20260504
```

This is promising but not proof because it is current-universe biased.

## Next Work

1. Build point-in-time archive universe coverage for the same TWAP contract.
2. Add slice-level forward paper execution.
3. Add slice-level Bybit demo sync.
4. Audit expected slices versus demo orders/fills/slippage/PnL.
5. Only then consider real-money design.

## Removed Scope

No legacy SignalEngine, old live runtime, blended composite stack, Telegram
trading bot, real-money exchange submission, or repo-local agent tooling belongs
in this repo.
