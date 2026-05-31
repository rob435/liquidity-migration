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

## Verdict (run 2026-05-30, 5950X full-PIT) — **PASS → K1 justified**

Ran both venues × two books: the **primary** `fixed_delay` (immediate +1h) ledger
(E1 `00_baseline`, age≥90 — isolates *detection* latency with no execution-wait
confound) and a **secondary** age-gated `quality_squeeze` book (E2 `02_age_min`,
age≥300 — the decision-relevant selection). Median `ceiling_uplift_bps`:

| book (entry policy) | venue | n | ALL | EARLY | RECENT | uplift/fade (ALL) |
|---|---|--:|--:|--:|--:|--:|
| fixed_delay, age≥90 | bybit | 763 | **993** | 957 | 1000 | 1.30 |
| fixed_delay, age≥90 | binance | 477 | **862** | 698 | 969 | 1.07 |
| age≥300, qsqueeze | bybit | 579 | **1041** | 1026 | 1071 | 1.22 |
| age≥300, qsqueeze | binance | 307 | **881** | 778 | 996 | 1.12 |

The daily-close (+1h) short enters a median **~8–11% below the event-day intraday
peak**, and the missed edge is **0.84–1.41× the entire realized fade** — i.e. even on the
winning shorts you leave as much on the table to lateness as you capture. **Positive in
BOTH venues AND BOTH splits, across BOTH books** → the pre-registered GATE (median
ceiling ≥ ~15 bps cost, both venues, both splits, not recent-only) is met by ~50–65×.
**Detection latency is a real lever at the daily→intraday scale** (contrast E1: fill
timing *within* a bar is not).

**Decomposition (K0b, `scripts/k0b_fade_decomposition.py`, fixed_delay):** the ceiling is
**~95–98% within-day-D giveback** (peak→daily-close; bybit within-share 0.94–0.96,
binance 0.96–0.98), with the **+1h overnight fill window contributing only ~19–64 bps**
(~2–6%). So faster *fills* capture almost nothing (E1 again); the prize requires detecting
the event **intraday, before the daily close, and entering then.** That is exactly the K1
lever — and its core difficulty, since the event is partly *defined* by the close.

**Honest bounds (this is necessary, not sufficient):** the ceiling assumes shorting the
exact intraday top — unachievable. A real PIT-causal intraday trigger fires somewhere
*below* the peak (the top is unknowable in real time, and the event isn't confirmed mid-pump),
so the realistic capture is a *fraction* of this ceiling. Quantifying that fraction under
the realistic engine is **K1**.

**Method notes / honest deviations from the frozen plan:**
- The plan said "the validated daily-event ledger" (unspecified cell). I used `fixed_delay`
  as the **primary** to isolate *detection* latency cleanly — `quality_squeeze` deliberately
  waits for the fade, which would inflate the ceiling with execution-wait and reopen the
  exact confound E1 closed. The age-gated `quality_squeeze` run is reported as a
  cross-check; it agrees (slightly larger, as expected).
- Fixed an **OOM bug** in the K0 script: `_intraday_highs` called `read_dataset(klines_1h)`
  which eagerly materialised the full ~23 GB set before filtering (would OOM this 32 GB box;
  also a #25 "all-or-nothing compute" violation). Replaced with a lock-free lazy scan
  (hive date/symbol file-pruning + column projection + predicate pushdown) — **numerically
  identical** (same symbol+ts filter, same per-(symbol,day) `max(high)`), perf-only.
- `klines_1h.ts_ms` is bar-**open** time; `date(ts−1ms)` buckets day-D's high window as
  opens `[01:00 D … 00:00 D+1]`, ending exactly at the +1h entry instant and excluding the
  entry bar — the correct, no-tautology ceiling window for an optimistic upper bound.

Per-trade tables: `~/SHARED_DATA/k0_{bybit,binance}_{fixed_delay,age300}.csv`;
decomposition JSON: `~/SHARED_DATA/k0b_{bybit,binance}_fixed_delay.json`.
Label: **EXPLORATORY** (read-only characterization; never promotion evidence).

**Next:** pre-register **K1** (rolling intraday-detection backtest vs the daily-close
baseline, PIT-causal, realistic engine, both venues, early/recent split). K2 (live WS)
stays explicit-operator-gated.
