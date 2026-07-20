# P0.4 coverage receipt — Binance backward-backfill frontier verification (2026-07-20)

Task: `docs/tail_risk_program.md` P0.4 / proposal D2 — "extend `binance_full_pit`
toward venue origin via `scripts/build_full_pit_binance.sh` (earlier
`BINANCE_START`), respecting upstream availability."

**Outcome: the backward frontier is already fully reached. No backward
extension is possible through the canonical builder, so no build was run.**
Acquisition-only verification; no kline/funding *values* beyond first/last
boundary timestamps were inspected, and no outcome (return/P&L) surface was
opened.

## What was verified (raw evidence: `probes.json`, SHA-256
`fd5a7e9e9900db1b7703f50b2a9a24e37d95c5e833b769b6a6a41cb719671e49`)

| Dataset | Local coverage | Upstream origin | Verdict |
| --- | --- | --- | --- |
| `klines_1h` + `archive_trade_manifest` | [2020-01-01 → 2026-07-10) | Vision monthly `futures/um` archives begin 2020-01 (HEAD 404 for 2019-09/10/12; S3 listing's earliest object is `BTCUSDT-1h-2020-01.zip`) | **at upstream floor** |
| `binance_usdm_funding` | first row 2019-09-10T08:00:00Z → [.., 2026-06-26) | venue launched 2019-09-08; 2019-09-10 08:00 is the first USD-M funding settlement | **at venue origin** |
| `binance_usdm_mark_price_1h` | first row 2019-12-23T11:00:00Z | REST `markPriceKlines` earliest = 1577098800000 (same bar) | **at REST origin** |
| `binance_usdm_index_price_1h` | first partition 2019-12-23 | REST `indexPriceKlines` earliest = same timestamp | **at REST origin** |
| `binance_usdm_premium_index_1h` | first partition 2019-12-24 | REST `premiumIndexKlines` earliest = 1577156400000 (2019-12-24T03:00Z) | **at REST origin** |

The stage-1 kline builder (`build-binance-oos`) takes no `--start`: it always
builds from the Vision archive origin, so the existing root already embodies
the earliest possible `BINANCE_START` for klines. The existing funding
coverage proves a prior build already ran stage 2 with the script-default
`BINANCE_START=2019-09-01`.

## What is *not* held, deliberately

- REST-only trade klines 2019-09-08T17:00Z → 2020-01-01 (earliest `fapi
  klines` open: 1567962000000) plus the single Vision daily file 2019-12-31.
  This ~115-day window is essentially BTCUSDT-only (ETHUSDT launched
  2019-11-27) and sits outside the canonical builder's Vision-only kline
  provenance contract. Acquiring it would require a separate, explicitly
  labelled research root (the `binance_vision_alt` pattern), not a mutation
  of `binance_full_pit`. Not done here; recorded as an option.
- Forward tails: ancillary datasets end earlier than klines (funding
  2026-06-26, mark/index/premium 2026-06-13, OI/taker REST 2026-06-12) —
  a *forward* freshness matter outside P0.4's backward scope, recorded here
  so nobody mistakes the root for uniformly-current through 2026-07-10.
- `binance_usdm_open_interest` / `binance_usdm_taker_flow_1h` begin
  2026-04-25: shallow REST endpoints that cannot be backfilled. Deep OI/LSR
  history for P2.x lives in `binance_usdm_metrics_5m` (Vision metrics,
  per-symbol parquet, separate fetcher).

## Why no build was launched

Re-running the builder would re-download the entire ~6.5-year monthly pair
into staging to publish byte-equivalent klines (its rebuild semantics), gain
zero backward rows on any dataset, and put an interruptible multi-hour
publish window on a shared root other sessions read. Evidence over motion:
the probes above are the complete deliverable.

Not evidence of: anything about strategy performance, PIT membership quality,
or the untouched status of any slice (that is P0.2's job).
