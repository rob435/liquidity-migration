# Bybit funding-rate timing semantics (verified 2026-07-19)

Question (from the V4 draft, deciding T-B's "next-rate" floor registrability):
is the funding rate charged at the NEXT settlement already fixed at the START
of the funding interval?

**Verdict: changed over time.**

- **Before 2022-06-30:** fixed one interval ahead (BitMEX-style). The rate
  charged at settlement T was computed over [T−16h, T−8h] and locked at T−8h —
  known deterministically for the whole following interval.
- **Since the 2022-06-30 → 2022-07-05 rollout** (all USDT + Inverse
  perpetuals): "settled immediately" — the rate charged at T is derived from
  the current interval [T−8h, T], updates every minute, and "may fluctuate
  before the end of the countdown cycle". It is final only at settlement.
- Precursor: five minor symbols (CELOUSDT, RVNUSDT, KLAYUSDT, SCUSDT,
  XEMUSDT) switched earlier, on 2022-04-24.

## Sources (fetched/verified 2026-07-19)

1. Current mechanism — Bybit Help Center, "Introduction to Funding Rate"
   (page last updated 2026-05-22),
   <https://www.bybit.com/en/help-center/article/Introduction-to-Funding-Rate>:
   "traders may check the funding rate, which will fluctuate in real-time
   until the upcoming funding timestamp. The funding rate is not fixed and is
   updated every minute…"; the average premium index uses linearly rising
   weights (1..480 across the interval), so late-interval prices dominate.
2. Old mechanism — archived Bybit Help article "What is funding rate and
   predicted rate?" (article 360039261114; Wayback snapshots 2020-05-10 and
   2021-10-25): "Funding rate calculated between 00:00–08:00 will be
   exchanged at 16:00 … traders may check on the funding rate which has been
   fixed for next funding timestamp (within 8 hours)."
3. Change point — Bybit announcement 2022-06-28, "Changes in Funding Rate:
   USDT Perpetual Contracts and Inverse Perpetual Contracts":
   "From 12 AM (midnight) UTC on June 30, 2022 to July 5, 2022, the funding
   fee … will gradually change into 'settled immediately' based on the rate
   derived from the current funding interval."
4. API — v5 `market/tickers` documents `fundingRate` without finality
   semantics; combined with (1), the ticker value under the current regime is
   a running value for the upcoming settlement (inference, flagged). v5
   `market/history-fund-rate` returns settled rates stamped at settlement
   time — PIT-safe at/after that timestamp.

## Consequences for this repository

- **T-B "next-rate" floor: not registrable.** Under the current (post-2022-07)
  regime the next settlement's rate is a fluctuating estimate at entry; any
  rule keyed on it is look-ahead-biased. The V3 T-B `next` convention results
  are PIT-valid only for the pre-2022-07 subsample.
- **The strictly-PIT `prev` convention** (last settled rate) is valid across
  the entire 2021–2026 sample and is what T-G uses.
- Funding intervals are per-symbol and time-varying (8h/4h/2h/1h, switches
  sometimes unannounced; extreme-rate conditions can auto-switch to 1h in the
  current regime, introduction date not pinned). Settlement cadence must be
  reconstructed from realized settlement history — which
  `scripts/research_v3/common.py` already does (labels are intentionally not
  read).
- The 2022-06-30 → 07-05 rollout was gradual; the exact per-symbol flip date
  is unpublished. Treat that window as ambiguous.
