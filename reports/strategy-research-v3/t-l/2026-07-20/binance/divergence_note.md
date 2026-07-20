# T-L Binance robustness pass — divergence note

Date: 2026-07-20. Same declared design as the Bybit run (same script,
`--venue binance`), events d0 in [2021-05-01, 2026-06-16] (right-censored
earlier than Bybit: Binance funding partitions end 2026-06-25). 659 events.
Venues are never pooled as independent (correlated listings, shared flows).

## What replicates (population level)

The 2024/2025 calendar flip is CROSS-VENUE ROBUST: unconditional short
d1/d2->d7 net45 pre-2025 +135/+105 bp -> post-2025 -147/-44 bp, with
funding turning against shorts (-96/-80 bp post-2025 vs -32/-20 pre).
Same sign structure as Bybit, smaller magnitudes.

## What does NOT replicate (the Bybit survivor cell)

Turnover-collapse short (tdec_lt30), d2 entry, net45 bp by era:
Binance e2122 -415 (n=6), e2324 -41 (n=38), e2526 -290 (n=86) —
NEGATIVE in all three eras where Bybit was positive in all three.
d1 entry: -1351 (n=3) / +688 (n=8) / +373 (n=28) — unstable, small n.
Threshold band 0.2-0.4: no stable positive region on Binance.

Verdict under the V4 double-verification habit (same sign on both venues
before banking a cell selected from a scan): the Bybit turnover-collapse
short FAILS cross-venue verification. T-L closes with no Lane-2 candidate.
The Bybit cell is recorded as either a selected fluke (nominal p=0.015
inside a ~90-pair scan) or Bybit-specific microstructure; this study
cannot distinguish the two, and neither is admissible.

## Limitations

- 3 Unicode-named Binance listings (e.g. Chinese-character tickers) were
  dropped as kline_gap_d0 because the study reader builds partition paths
  from raw symbols without the `symbol_codec` percent-encoding; counted,
  negligible (3/659), direction of bias unknown but bounded.
- 113 pre-floor symbols (Binance universe predates the 2021-05-01 floor);
  the floor deliberately preserves G1/G2 unread.
- No 1m execution-cost read on Binance (no local Binance 1m render root
  covering listing weeks; `binance_vision_alt` covers render-mapped
  symbols only and was not read here).
