# Continuous Fade 5m Data Backfill - 2026-06-27

## Purpose

Backfill 5-minute kline data across the continuous-fade validation sample so
15m/30m entry timing, next-red candle, failed-high, adverse-limit, and path
diagnostics can be run without narrowing to trade-only symbols.

This is data-root maintenance only. It is not alpha evidence, not a parameter
verdict, and not live-size approval. No live orders, production credentials, or
mainnet paths were used.

## Tooling

- Script: `scripts/backfill_5m_klines.py`
- Tests: `tests/test_backfill_5m_klines.py`
- Dataset written: canonical `klines_5m` under each full-PIT root.
- Source:
  - Bybit: v5 public market kline API, manifest-gated.
  - Binance: `data.binance.vision` USD-M 5m kline archives, manifest-gated.

The script checks `archive_trade_manifest` first and only downloads missing or
short `(date, symbol)` partitions. It writes JSON receipts under each data root's
`reports/` directory.

## Window

| Venue | Root | Window | PIT manifest basis |
| --- | --- | --- | --- |
| Bybit | `~/SHARED_DATA/bybit_full_pit` | 2023-04-01..2026-06-25 | `archive_trade_manifest` through 2026-06-25 |
| Binance | `~/SHARED_DATA/binance_full_pit` | 2023-04-01..2026-06-24 | `archive_trade_manifest` through 2026-06-24 |

## Initial Gap

Fast partition-presence audit (`--min-existing-bars 1`):

| Venue | Missing symbols | Missing symbol-days |
| --- | ---: | ---: |
| Bybit | 634 | 72,407 |
| Binance | 773 | 458,874 |

## Backfill Receipts

Rows written during this pass:

| Venue | Rows written | Fetch failures after final retry |
| --- | ---: | ---: |
| Bybit | 21,077,208 | 0 |
| Binance | 132,161,024 | 0 |

Key receipts:

- Bybit final presence audit:
  `~/SHARED_DATA/bybit_full_pit/reports/backfill_5m_klines_bybit_20260627T112706Z.json`
- Bybit strict retry:
  `~/SHARED_DATA/bybit_full_pit/reports/backfill_5m_klines_bybit_20260627T113221Z.json`
- Bybit final strict audit:
  `~/SHARED_DATA/bybit_full_pit/reports/backfill_5m_klines_bybit_20260627T113606Z.json`
- Binance final presence audit:
  `~/SHARED_DATA/binance_full_pit/reports/backfill_5m_klines_binance_20260627T112706Z.json`
- Binance Unicode-symbol retry:
  `~/SHARED_DATA/binance_full_pit/reports/backfill_5m_klines_binance_20260627T112401Z.json`
- Binance strict retry:
  `~/SHARED_DATA/binance_full_pit/reports/backfill_5m_klines_binance_20260627T113221Z.json`
- Binance final strict audit:
  `~/SHARED_DATA/binance_full_pit/reports/backfill_5m_klines_binance_20260627T113606Z.json`

The first Binance final chunk hit 36 URL failures on three non-ASCII symbols.
The bug was raw Unicode path construction; percent-encoding the Binance Vision
URL path fixed it, and the retry wrote the remaining 9,504 rows.

## Final Coverage

Partition-presence audit after retries (`--min-existing-bars 1`):

| Venue | Missing symbols | Missing symbol-days |
| --- | ---: | ---: |
| Bybit | 0 | 0 |
| Binance | 0 | 0 |

Strict 288-bars/day audit after one strict retry:

| Venue | Symbols with partial source days | Partial symbol-days |
| --- | ---: | ---: |
| Bybit | 649 | 781 |
| Binance | 25 | 25 |

Interpretation: every PIT manifest symbol-day in the sample now has a 5m
partition. The remaining strict-density rows are partial exchange source days
after a retry, not absent partitions. Do not fabricate dense 5m paths from 1h
bars; downstream timing/path research must either tolerate these partial source
days explicitly or exclude them with a written coverage rule.

## First Use

The first consumer was
`research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/`.
It produced 15m delay, 30m delay, next-red 15m, and forward-path diagnostics
with a complete 24h 5m path rule. The timing variants did not improve the
signal-level diagnostic versus immediate entry on either venue.

This backfill does not make any timing/stop diagnostic candidate evidence. Any
rule change still needs a portfolio replay with direct feature/data timestamps
and a decision rule.
