# Data Roots

Current as of 2026-05-18.

## Canonical Research Root

Use this root for serious full-PIT research:

```text
~/SHARED_DATA/bybit_fullpit_1h
```

This is the canonical shared Bybit research archive. The current manifest and
1h kline set covers `2023-05-03` through `2026-05-17`, with commands using
`--end 2026-05-18` as the end-exclusive boundary for completed bars.

Current contents:

```text
archive_trade_manifest: 371,769 rows, 465 symbols, 2023-05-03..2026-05-17
klines_1h:              8,922,456 rows, 465 symbols, 2023-05-03..2026-05-17
funding:                1,446,834 rows, 465 symbols, 2023-05-03..2026-05-18
open_interest:            894,137 rows, 293 symbols, 2025-05-03..2026-05-18
mark/index/premium 1h:  3,058,421 rows each, 388 symbols, 2025-05-03..2026-05-18
```

The recent 30-day Bybit-native audit for `2026-04-18` to `2026-05-18` is full
for klines, funding, open interest, mark price, index price, and premium index:

```text
~/SHARED_DATA/bybit_fullpit_1h/reports/canonical_recent_30d_coverage_20260418_20260518_v3_shared_root/data_layer_audit.md
```

Signed trade flow is still missing. Do not claim native leverage-flow evidence
until `signed_flow_1h` exists and passes `data-layer-audit`.

## Live Demo Root

The live Bybit demo runner intentionally uses a separate operational root:

```text
data/bybit-demo-event
```

Do not point the live demo order/trade ledgers at the full-PIT research root.
The demo root contains forward kline cache, order ledgers, trade ledgers, cycle
reports, and risk-watchdog reports.

## Out-of-Sample Roots

Two pre-2023 PIT roots exist for genuine out-of-sample validation — both predate
the canonical archive start (`2023-05-03`), and both reconstruct point-in-time
membership from sources that include delisted/migrated symbols (no
survivorship-biased `exchangeInfo`).

```text
~/SHARED_DATA/bybit_oos_pre2023    Bybit USD-M perps, 2021-01..2023-05
                                   source: public.bybit.com/trading archive
~/SHARED_DATA/binance_oos_pit      Binance USD-M perps, 2020-01..2023-04
                                   source: data.binance.vision monthly archive
                                   (includes 25 delisted symbols)
```

These roots are **not committed** (data, not code). Rebuild them on any machine:

```bash
bash scripts/build_oos_roots.sh
```

That script builds the Bybit OOS root via `archive-manifest` +
`archive-download-klines-1h-api`, builds the Binance OOS root via
`python -m liquidity_migration.binance_vision build-binance-oos`, and
coverage-filters both manifests so they pass the full-PIT universe check.
Funding/OI/mark are intentionally not filled for the OOS roots; the strategy
degrades gracefully without them. Expect a 10-25 minute run.

These OOS windows have been examined repeatedly in research and are no longer
pristine — treat them as validation, not first-look OOS.

## Retired Roots

Do not use ad hoc current-universe or temporary recent roots for promotion
evidence. Current-universe 120-symbol research is biased by construction unless
the membership is point-in-time. A live `exchangeInfo` snapshot is never an
acceptable cross-venue PIT source — see `docs/backtesting_errors_we_never_repeat.md`.
