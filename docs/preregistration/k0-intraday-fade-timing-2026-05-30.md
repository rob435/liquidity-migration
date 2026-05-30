# Pre-registration — K0: intraday-fade-timing upside ceiling

**Date:** 2026-05-30 · **Stage:** EXPLORATORY (read-only characterization; NOT promotion evidence)
**Plan:** `docs/research_plan_intraday_kernel.md` (the intraday-detection kernel, phase K0)
**Standard:** `docs/backtesting_errors_we_never_repeat.md` · `docs/parameter_pre_registration.md`
**Run on:** the 5950X full-PIT roots (read-only; the 16 GB box can't hold the klines).

## Hypothesis (the kernel's binding assumption, tested before any build)

The deployed strategy detects the liquidity-migration event on the **daily-close roll**
and enters +1h. **H1:** that entry is systematically *late* — a material part of the
eventual fade is already gone by the daily-close entry — so detecting the event
**intraday** (off the WS stream) could short higher and capture more fade.
**H0 (null):** the daily-close entry is not systematically late → detection latency is a
non-lever (consistent with E1) → the K1/K2 build is not justified.

## Method (pre-registered, frozen before the run)

`scripts/k0_intraday_fade_timing_precheck.py --report-dir <daily-event report> --root <venue full-PIT> --venue <bybit|binance>`

For every **short** in the validated daily-event ledger (`volume_event_best_trades.csv`):
- trading day `D = date(entry_signal_ts_ms − 1 ms)` (the day the event summarises);
- `intraday_high` = max 1h-kline high on day D for that symbol;
- `ceiling_uplift_bps = (intraday_high − daily_entry_price) / daily_entry_price × 1e4`
  — the **upper bound** on extra short edge from faster detection (you can never beat
  shorting the exact intraday top);
- `realized_fade_bps = (daily_entry_price − exit_price) / daily_entry_price × 1e4`.

Report median/mean/p75 `ceiling_uplift_bps` and the `uplift/fade` ratio, **per venue**,
split **EARLY (<2025-06-01) / RECENT (≥2025-06-01)**. Run **both venues** (bybit
`~/SHARED_DATA/bybit_full_pit`, binance `~/SHARED_DATA/binance_full_pit`).

This is an explicit **ceiling** (optimistic — assumes detection at the exact intraday
high). A realistic intraday detector enters somewhere below the top, so K0-positive is
**necessary, not sufficient**; the realistic test is K1.

## Decision rule (pre-committed)

- **GATE → K1 (build the intraday detector):** median `ceiling_uplift_bps` is materially
  positive (≥ ~the round-trip cost, ~15 bps) on **BOTH venues** AND in **BOTH the EARLY
  and RECENT** splits (not a recent-alt-bear artifact — the c2b lesson). The `uplift/fade`
  ratio should show the missed edge is a non-trivial share of the realized fade.
- **FALSIFIER → STOP:** the ceiling is ~0 / below cost on either venue, OR positive only
  in RECENT (regime-conditional). Then detection latency is a non-lever (E1 holds at the
  daily→intraday scale too); file the negative verdict, keep the daily-close strategy +
  age gate, do **not** build K1/K2.

## What would make this run INVALID

- A venue/root mismatch (ledger from one venue, klines from the other) — the script
  aborts if no trades match intraday klines.
- Treating a positive ceiling as tradeable alpha (it is an upper bound, not a strategy).
- Citing it as promotion/OOS evidence (it is EXPLORATORY by construction).

## Status

PENDING — run on the 5950X (both venues), then record the verdict here and roll the
headline into `docs/research_summary.md`; if it passes, pre-register K1.
