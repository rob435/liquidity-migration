# Funding guard false-positive: `funding_interval_min` mislabel on real sub-8h funding

**Date:** 2026-06-15 · **Author:** W6 loop (research) · **Status:** finding + research-scoped
workaround in place; proper upstream fix is OPERATOR-gated.

## Summary

`run_continuous_event_research` on `~/SHARED_DATA/bybit_full_pit` now aborts with
`RuntimeError: funding dataset ... looks like an hourly SNAPSHOT scrape` from
`_assert_funding_one_per_settlement` (added in audit commit `7d39d61`, item
`cost-funding-5`). This blocks **every bybit engine backtest** whose window touches
~2024+ (W5/W6 sweeps, the equity-curve tool, the forward replay).

**It is a FALSE POSITIVE. The funding data is correct.** ~89 bybit symbols genuinely
settle **sub-8h** (Bybit per-symbol / dynamically-shortened funding intervals). The
guard compares each symbol's observed settlement cadence against the dataset's
`funding_interval_min`, which is stamped with the **current** interval (480) on **all**
history — so a real 2h/4h/1h cadence is misread as oversampling.

## Evidence (verified against the authoritative endpoint, 2026-06-15)

Compared local rows to Bybit `get_funding_rate_history` (`fundingRateTimestamp`, one
row per settlement) and `instruments.fundingInterval`:

| Symbol | instruments interval | local vs authoritative | distinct rates |
|---|---|---|---|
| EGLDUSDT (2022 episode) | 480 now (was 1h in 2022) | exact match, distinct realized rates | varies |
| ANIMEUSDT | **240** (4h) | 12–13 rows/day, exact match | capped day |
| 1000TOSHIUSDT | 240 | 13/13 rate match, 0 extra local | 2 |
| ZETAUSDT | 240 | 25/25 match, 0 extra | 1 (capped) |
| HYPERUSDT | 240 | 49/49 match, 0 extra | **14** |
| SOPHUSDT | 240 | 49/49 match, 0 extra | **15** |
| MOVEUSDT | 240 | 25/25 match, 0 extra | **19** |

The high distinct-rate counts (14–19 distinct realized rates over 49 stamps) are
**impossible for a snapshot scrape** (which repeats one predicted rate). BTCUSDT stays
8h, so this is **not** a uniform hourly ticker scrape. Diagnostic scope: **89 / 785**
bybit symbols flagged across full history (mostly 2024+ listings; a few old majors —
EGLD/ENJ/FLOW/MASK — had 2022 sub-8h episodes).

## Why the funding CHARGE is already correct

`trade_lifecycle._funding_lookup` / `_perp_funding_return` sum the **raw `funding_rate`**
at **every distinct settlement stamp** between entry and exit (exact-stamp dedup), and
**never read `funding_interval_min`**. Its own docstring documents "distinct ts_ms ARE
distinct settlements" — i.e. the charge is correct precisely *because* the data is
one-row-per-real-settlement. So no backtest's funding charge is wrong; only the guard
trips, and `funding_rate_8h_equiv` (= `funding_rate * 480/interval`, used as the W6
funding squeeze feature, **not** by the charge) is understated for sub-8h symbols.

## Workaround in place (research-scoped, nothing pushed)

`scripts/w6_squeeze_proxy_sizing.py` monkeypatches a corrected guard that fires only on
**clearly sub-60min cadence** (< 55min) — real perp funding never settles more often
than hourly, so this still catches a genuine sub-hourly snapshot scrape while passing
verified-correct sub-8h funding. Scoped to that research script; the engine and the live
demo path are untouched, and nothing is committed to `liquidity_migration/**`.

## Recommended upstream fix (OPERATOR-gated)

1. **Relabel `funding_interval_min`** in the funding root from the observed
   inter-settlement gap per row (the data already carries one row per real settlement),
   and **recompute `funding_rate_8h_equiv`**. This makes the dataset honest, fixes the
   8h-equiv feature, and lets the guard pass legitimately. Large shared-root rewrite —
   back up first and mind parallel sessions (SHARED_DATA volatility).
2. **And/or** land the sub-60min floor in the engine guard
   (`continuous_events._assert_funding_one_per_settlement`): only flag cadence finer than
   the real-funding floor (and/or pass the authoritative per-symbol interval, which
   `_funding_lookup` already supports via `interval_by_symbol`). Engine change →
   auto-deploys on push; operator-gated.

Until one of these lands, every bybit research backtest must apply the corrected guard.
