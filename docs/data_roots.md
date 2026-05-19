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

## Retired Roots

Do not use ad hoc current-universe or temporary recent roots for promotion
evidence. Current-universe 120-symbol research is biased by construction unless
the membership is point-in-time.
